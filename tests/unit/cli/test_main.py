"""Unit tests for the Forge CLI entry point.

After the output-and-error-handling refactor, the CLI has two
testable layers, and this file tests each layer at its own layer:

1. **The app** — built by ``create_app()`` and exercised via
   ``Typer.CliRunner``. These tests prove the commands, help text, and
   option parsing work.

2. **The entry point** — ``main()``. Tested via ``subprocess`` in one
   integration test that exercises the real ``python -m forge.cli``
   pathway.

The exception handler (``forge.cli.errors.handle_exception``) and the
output helpers (``forge.cli.ui.print_*``) are tested in their own
files:

- ``tests/unit/cli/test_errors.py`` — the handler's contract: every
  ``ForgeError`` subclass maps to its exit code; no traceback ever
  leaks; ``--debug`` re-raises.
- ``tests/unit/cli/test_ui.py`` — the voice: every helper writes to
  the correct stream with the correct prefix.

Testing each layer in its own file keeps the failure mode clear. A
failure in ``test_errors.py`` points at the handler; a failure in
``test_ui.py`` points at the helpers; a failure in ``test_main.py``
points at the entry point or the app factory. There is no ambiguity
about where to look.

Coverage map
------------
Acceptance criteria:

1. ``forge --help`` displays a usage message and exits 0.
2. ``forge --version`` displays the version from package metadata.
3. ``forge ping`` prints ``pong``.
4. The root help contains a description.
5. The CLI is invokable via ``python -m forge.cli``.

Additional behaviour:

6. The version callback delegates to ``get_version`` verbatim.
7. The ``--debug`` flag is parsed and stored on ``ctx.obj``.

Design principles
-----------------
- **Shared fixtures first.** ``cli_runner``, ``forge_app``, and
  ``forge_executable`` come from ``tests/conftest.py``. Local fixtures
  are declared only when a test needs a modification (e.g.,
  registering a ``_probe_*`` command on a private copy of the app).
- **No Click dependency.** Typer 0.28 does not depend on Click. This
  file imports only from ``typer`` and ``typer.testing``.
- **Every test maps to a documented behaviour.** If a test fails, the
  docstring tells the next contributor exactly what broke and why.
- **Command-agnostic.** The CLI's commands are registered by the
  registry (``forge.cli.registry``), not by ``create_app()``. This
  file tests the *contract* the registry guarantees (every command
  appears in ``forge --help`` and is invokable), not the registry's
  internals. Registry internals are tested in
  ``tests/unit/cli/test_registry.py``.
- **Stream-agnostic.** This file does not assert on which stream a
  message goes to. That is ``test_ui.py``'s job. This file asserts on
  the *content* of the output, which is the app's contract.
"""

from __future__ import annotations

import subprocess

import pytest
import typer
from typer.testing import CliRunner

from forge.cli.main import create_app


# =============================================================================
# Local fixtures
# =============================================================================
# The shared ``cli_runner`` and ``forge_app`` fixtures live in
# ``tests/conftest.py``. The fixture below is a local override used only
# when a test needs a modification — for example, a test that registers
# a ``_probe_*`` command on a private copy of the app so the command
# does not leak into other tests.
#
# If you only need to invoke the CLI, use the shared ``forge_app``
# fixture. If you need to register commands on the app, use the local
# ``app`` fixture below.
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
# Layer 1 — the app (create_app + CliRunner)
# =============================================================================


# -----------------------------------------------------------------------------
# Acceptance criterion 1 — forge --help displays a usage message
# -----------------------------------------------------------------------------
class TestHelp:
    """Tests for ``forge --help``."""

    @pytest.mark.unit()
    def test_displays_usage(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """``forge --help`` prints a usage message and exits 0.

        Explicit help is a success. Click/Typer exit with code 0 when
        the user asks for help.
        """
        result = cli_runner.invoke(forge_app, ["--help"])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output

    @pytest.mark.unit()
    def test_short_flag(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """The short form ``-h`` is equivalent to ``--help``."""
        result = cli_runner.invoke(forge_app, ["-h"])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output

    @pytest.mark.unit()
    def test_lists_all_commands(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """The root help lists every registered command.

        This is the "table of contents" contract: if a command module
        is added to ``forge.cli.commands/`` but the registry skips it
        (e.g., because it violates the contract), it will not appear
        in ``forge --help``. That would be a silent failure, so this
        test asserts the presence of every command the project
        *promises*.

        The expected list is explicit (not computed from the package)
        so that removing a command is caught as a regression, not
        silently accepted as "the package no longer has it."
        """
        result = cli_runner.invoke(forge_app, ["--help"])
        assert result.exit_code == 0, result.output
        for command in ("list", "create", "validate", "verify", "doctor", "ping"):
            assert command in result.output, (
                f"Command {command!r} is registered but missing from "
                f"`forge --help`. Check that the corresponding module "
                f"under `src/forge/cli/commands/` exists and exposes a "
                f"`typer.Typer` as `app`."
            )

    @pytest.mark.unit()
    def test_contains_description(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """The root help contains the app description from ``create_app()``.

        A CLI without a description is a CLI nobody understands. This
        test pins the description so that a future contributor cannot
        quietly reduce it to ``None``.
        """
        result = cli_runner.invoke(forge_app, ["--help"])
        assert result.exit_code == 0, result.output
        assert "engineering foundations" in result.output.lower()


# -----------------------------------------------------------------------------
# Acceptance criterion 2 — forge --version displays the package version
# -----------------------------------------------------------------------------
class TestVersion:
    """Tests for ``forge --version``."""

    @pytest.mark.unit()
    def test_displays_package_version(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """``forge --version`` prints the version from package metadata.

        The version shown must match
        ``importlib.metadata.version("forge-cli")`` exactly. This is the
        single source of truth — no hard-coded strings can drift from
        it.
        """
        from importlib.metadata import version as pkg_version

        expected = pkg_version("forge-cli")
        result = cli_runner.invoke(forge_app, ["--version"])
        assert result.exit_code == 0, result.output
        assert "forge, version" in result.output
        assert expected in result.output

    @pytest.mark.unit()
    def test_short_flag(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """The short form ``-V`` is equivalent to ``--version``."""
        result = cli_runner.invoke(forge_app, ["-V"])
        assert result.exit_code == 0, result.output
        assert "forge, version" in result.output

    @pytest.mark.unit()
    def test_is_eager(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """``--version`` runs even when an invalid subcommand is also given.

        ``--version`` is marked ``is_eager=True`` in the root callback,
        so ``forge --version nonsense`` prints the version and exits 0
        without trying to dispatch ``nonsense``. This matches the
        behaviour of ``git --version``, ``docker --version``, and every
        other serious CLI.
        """
        result = cli_runner.invoke(forge_app, ["--version", "nonsense-command"])
        assert result.exit_code == 0, result.output
        assert "forge, version" in result.output

    @pytest.mark.unit()
    def test_callback_prints_fallback_verbatim(
        self,
        cli_runner: CliRunner,
        forge_app: typer.Typer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``forge --version`` prints whatever ``get_version()`` returns.

        The CLI does not implement the version fallback — it delegates
        to ``forge.core.version.get_version``. That module is
        responsible for the four-step fallback chain (metadata →
        ``_version.py`` → ``FORGE_VERSION`` → ``"0.0.0.dev0"``), and it
        is tested exhaustively in
        ``tests/unit/core/test_version.py::TestFallbackChain``.

        This test verifies the *delegation*: when the resolver returns
        the dev fallback, the CLI prints it verbatim — no prefix
        stripping, no reformatting, no swallowing. If a future
        refactor changed the print format or short-circuited the
        resolver, this test would catch it.

        The ``get_version`` symbol is patched *in the CLI module*,
        where it is imported by name. Patching
        ``forge.core.version.get_version`` would not affect the CLI,
        because the CLI holds its own reference to the function.
        """
        monkeypatch.setattr(
            "forge.cli.main.get_version",
            lambda: "0.0.0.dev0",
        )

        result = cli_runner.invoke(forge_app, ["--version"])

        assert result.exit_code == 0, result.output
        assert "0.0.0.dev0" in result.output
        # Verify the exact format the CLI promises: "forge, version <v>".
        assert "forge, version 0.0.0.dev0" in result.output


# -----------------------------------------------------------------------------
# Acceptance criterion 3 — forge ping prints pong
# -----------------------------------------------------------------------------
class TestPing:
    """Tests for ``forge ping``."""

    @pytest.mark.unit()
    def test_prints_pong(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """``forge ping`` prints ``pong`` and exits 0.

        The ``ping`` command is the cheapest possible health check: if
        it works, the CLI is installed, importable, and correctly
        wired to Typer. If it does not, the problem is installation,
        not the command the user was actually trying to run.

        This test is *command-agnostic about the registry*. The
        ``ping`` command may be defined inline in ``create_app()``, as
        a module in ``forge.cli.commands/``, or as a plugin. What
        matters is that ``forge ping`` works. If the registry is ever
        refactored, this test does not need to change.
        """
        result = cli_runner.invoke(forge_app, ["ping"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "pong"

    @pytest.mark.unit()
    def test_is_documented_in_help(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """``forge ping --help`` documents the command's purpose."""
        result = cli_runner.invoke(forge_app, ["ping", "--help"])
        assert result.exit_code == 0, result.output
        assert "pong" in result.output.lower()


# -----------------------------------------------------------------------------
# Additional behaviour — no arguments, unknown commands
# -----------------------------------------------------------------------------
class TestUsageErrors:
    """Tests for usage errors (no command, unknown command)."""

    @pytest.mark.unit()
    def test_no_args_shows_help_and_exits_two(
        self, cli_runner: CliRunner, forge_app: typer.Typer
    ) -> None:
        """``forge`` with no args shows help and exits 2 (usage error).

        This is a deliberate contract, not an oversight.
        ``no_args_is_help`` prints help when no command is given, but
        Typer still treats the invocation as a *usage error* and exits
        with code 2. The distinction between this and ``forge --help``
        (exit 0) is a useful signal to scripts: a script that
        accidentally drops the command name should fail loudly, not
        silently succeed.
        """
        result = cli_runner.invoke(forge_app, [])
        assert result.exit_code == 2, result.output
        assert "Usage:" in result.output

    @pytest.mark.unit()
    def test_unknown_command_exits_two(self, cli_runner: CliRunner, forge_app: typer.Typer) -> None:
        """An unknown command exits with code 2 (usage error)."""
        result = cli_runner.invoke(forge_app, ["nonsense-command"])
        assert result.exit_code == 2, result.output


# -----------------------------------------------------------------------------
# --debug flag is parsed and stored on ctx.obj
# -----------------------------------------------------------------------------
class TestDebugFlag:
    """Tests for the ``--debug`` flag's parsing.

    The flag's *effect* (re-raising exceptions) is tested in
    ``test_errors.py::TestDebugMode``. This class tests only that the
    flag is *parsed* and stored — the contract the rest of the CLI
    depends on.
    """

    @pytest.mark.unit()
    def test_is_stored_on_context(self, cli_runner: CliRunner, app: typer.Typer) -> None:
        """``--debug`` stores ``True`` on ``ctx.obj["debug"]``.

        The flag itself is a no-op in this task; it is consumed by the
        exception handler in ``main()``. This test pins down the
        contract that the flag is parsed and stored, so a future
        refactor cannot silently drop it.

        Uses the *local* ``app`` fixture (not ``forge_app``) because
        it registers a ``_probe_debug`` command on the app — a
        modification that must not leak into other tests.
        """

        @app.command("_probe_debug")
        def _probe_debug(ctx: typer.Context) -> None:
            typer.echo(f"debug={ctx.obj.get('debug')}")

        result = cli_runner.invoke(app, ["--debug", "_probe_debug"])
        assert result.exit_code == 0, result.output
        assert "debug=True" in result.output

    @pytest.mark.unit()
    def test_defaults_to_false(self, cli_runner: CliRunner, app: typer.Typer) -> None:
        """Without ``--debug``, ``ctx.obj["debug"]`` is ``False``."""
        # Reuse the same probe from the previous test. Registering it
        # twice would conflict, so this test creates its own app and
        # its own probe.
        probe_app = create_app()

        @probe_app.command("_probe_debug")
        def _probe_debug(ctx: typer.Context) -> None:
            typer.echo(f"debug={ctx.obj.get('debug')}")

        result = cli_runner.invoke(probe_app, ["_probe_debug"])
        assert result.exit_code == 0, result.output
        assert "debug=False" in result.output


# =============================================================================
# Layer 2 — the entry point (main)
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
