"""Forge CLI entry point.

This module is the single, canonical entry point for the ``forge``
command. It is intentionally minimal: it creates the Typer application,
registers the root callback (for global options), mounts the command
groups, and hands control to Typer. It contains no business logic.

Design principles
-----------------
1. **Thin entry point.** ``main()`` does three things: create the app,
   register commands, and call the app. Anything else belongs in a
   module under ``forge.cli.commands`` or ``forge.core``.
2. **Version from metadata.** ``--version`` reads the installed package
   version via ``importlib.metadata``. No hard-coded strings, no drift.
   The import is at module level so tests can monkeypatch it without
   touching ``importlib.metadata`` globally.
3. **Exit codes are contractual.** Every documented failure mode maps to
   an ``ExitCode`` enum value. The enum lives in
   ``forge.core.exceptions``.
4. **Error handling at the boundary.** Unhandled exceptions are caught
   here and rendered as user-friendly messages, never as raw
   tracebacks. The ``--debug`` flag re-raises them for contributors.
5. **Testable.** ``main()`` can be invoked via ``Typer.CliRunner``
   without launching a subprocess, so unit tests run in milliseconds.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, NoReturn

import typer
from rich.console import Console

from forge.core.exceptions import ExitCode, ForgeError

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
PACKAGE_NAME = "forge-cli"
"""The distribution name on PyPI. Distinct from the import name (``forge``)."""

# A single, module-level console so every message flows through the same
# Rich configuration. Rich's ``Console`` handles TTY detection, colour,
# width, and ``--no-color`` internally — no need to reimplement any of it.
console = Console()


# -----------------------------------------------------------------------------
# Version resolution
# -----------------------------------------------------------------------------
def _resolve_version() -> str:
    """Return the installed package version, or a dev fallback.

    The primary path reads the version that was actually installed via
    ``importlib.metadata``. This guarantees the version shown by
    ``forge --version`` always matches the version on disk, even if the
    source tree was checked out at a different commit.

    The fallback (``"0.0.0.dev0"``) covers the case where Forge is run
    directly from a source checkout without an editable install. In that
    case, ``importlib.metadata`` cannot find the package, and we fall
    back to a clearly-marked dev version rather than crashing.

    ``version`` is imported at module level (not inside this function)
    so tests can monkeypatch ``forge.cli.main.version`` to simulate a
    missing package without touching ``importlib.metadata`` globally.
    """
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "0.0.0.dev0"


# -----------------------------------------------------------------------------
# Version callback
# -----------------------------------------------------------------------------
def _version_callback(value: bool) -> None:
    """Print the resolved version and exit, if ``--version`` was passed.

    Called eagerly by Typer *before* any subcommand runs, so that
    ``forge --version`` works even if the subcommand is invalid.
    """
    if value:
        console.print(f"forge, version {_resolve_version()}")
        raise typer.Exit(code=ExitCode.SUCCESS)


# -----------------------------------------------------------------------------
# Root callback
# -----------------------------------------------------------------------------
def _root_callback(
    ctx: typer.Context,
    version_requested: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Re-raise exceptions with full tracebacks (for contributors).",
            envvar="FORGE_DEBUG",
            show_envvar=True,
        ),
    ] = False,
) -> None:
    """Global options for every Forge command.

    Runs once, before any subcommand, and stores shared state on
    ``ctx.obj`` so subcommands can read it (e.g., ``ctx.obj["debug"]``).
    """
    # Store shared state on the context object. Subcommands read from it.
    # Using a dict (rather than a dataclass) keeps the callback trivial;
    # if shared state grows, promote it to a dataclass in a later task.
    ctx.obj = {"debug": debug}


# -----------------------------------------------------------------------------
# Exception handling
# -----------------------------------------------------------------------------
def _handle_exception(exc: BaseException, *, debug: bool) -> NoReturn:
    """Render a user-facing error and exit with the correct exit code.

    ``ForgeError`` subclasses are rendered as a single, human-readable
    line with an optional hint, and exit with their documented exit code.
    ``KeyboardInterrupt`` and ``typer.Abort`` are rendered as a dimmed
    "Cancelled." message and exit 1 (user error). Unexpected exceptions
    are rendered as a generic "internal error" line and exit with
    ``ExitCode.INTERNAL_ERROR``. In ``--debug`` mode, all of the above
    are re-raised so the developer sees the traceback.
    """
    if debug:
        raise exc

    # --- User-facing ForgeError ---------------------------------------------
    if isinstance(exc, ForgeError):
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        hint = getattr(exc, "hint", None)
        if hint:
            console.print(f"[dim]Hint: {hint}[/dim]", highlight=False)
        raise typer.Exit(code=exc.exit_code) from None

    # --- User cancellation --------------------------------------------------
    if isinstance(exc, KeyboardInterrupt | typer.Abort):
        console.print("\n[dim]Cancelled.[/dim]", highlight=False)
        raise typer.Exit(code=ExitCode.USER_ERROR) from None

    # --- Unexpected exception -----------------------------------------------
    # Do not leak implementation details (class names, stack frames) to
    # the user. Point at the issue tracker so they have a next step.
    console.print(
        "[red]Error:[/red] An unexpected internal error occurred.",
        highlight=False,
    )
    console.print(
        "[dim]Run with --debug to see the full traceback, or "
        "report this at https://github.com/thapelomagqazana/forge-cli/issues[/dim]",
        highlight=False,
    )
    raise typer.Exit(code=ExitCode.INTERNAL_ERROR) from None


# -----------------------------------------------------------------------------
# Application factory
# -----------------------------------------------------------------------------
def create_app() -> typer.Typer:
    """Build and return the root Typer application.

    This is a *factory*, not a module-level constant. That makes the app
    re-creatable for tests and for future multi-app scenarios (e.g., a
    separate ``forge-admin`` CLI that reuses the same commands).

    Returns:
        A fully-configured ``typer.Typer`` instance with all commands
        mounted and the root callback installed.
    """
    app = typer.Typer(
        name="forge",
        help=(
            "Forge CLI — create, verify, and maintain engineering foundations.\n\n"
            "Forge helps software teams create new projects from approved "
            "blueprints, verify that generated projects meet their declared "
            "baseline checks, and maintain consistent engineering standards "
            "over time."
        ),
        # The root callback handles --version and --debug. Subcommands
        # are responsible for their own arguments.
        callback=_root_callback,
        # Do not invoke the callback when no subcommand is given; instead,
        # show the help text. This makes `forge` with no args useful
        # rather than an error. Note: Typer still exits 2 (usage error)
        # because no command was given — the help is a courtesy, not a
        # successful invocation.
        no_args_is_help=True,
        # Enable shell completion (Typer emits the instructions for the
        # user's shell). Completion is a "polish" feature, but it pays
        # for itself the first time a user types `forge cl<TAB>`.
        add_completion=True,
        # Disable Rich markup in help text; help is plain text so it
        # renders predictably in every terminal, including CI logs.
        rich_markup_mode=None,
        # Keep the help output narrow enough to fit an 80-column terminal.
        context_settings={"help_option_names": ["-h", "--help"]},
    )

    # -------------------------------------------------------------------------
    # Command registration
    # -------------------------------------------------------------------------
    # Commands are registered here, not via decorators, so that this module
    # remains the single source of truth for "what commands exist". Each
    # command lives in its own module under `forge.cli.commands`.
    #
    # The imports are local to this function to avoid a circular import:
    # `commands/*` modules import `main` indirectly through `forge.cli`.
    from forge.cli.commands import create, doctor, validate, verify
    from forge.cli.commands import list as list_cmd

    app.add_typer(list_cmd.app, name="list")
    app.add_typer(create.app, name="create")
    app.add_typer(validate.app, name="validate")
    app.add_typer(verify.app, name="verify")
    app.add_typer(doctor.app, name="doctor")

    # -------------------------------------------------------------------------
    # Sanity-check command
    # -------------------------------------------------------------------------
    @app.command(name="ping", help="Print 'pong' to confirm the CLI is working.")
    def ping() -> None:
        """Print 'pong' to confirm the CLI is working.

        This command exists for two reasons:

        1. It is the fastest way to verify that a fresh install works
           end-to-end. If ``forge ping`` fails, the problem is
           installation, not the command the user was trying to run.
        2. It is the simplest possible Typer command, so it doubles as a
           reference for future contributors adding their own commands.

        It takes no arguments and does no I/O beyond ``print``.
        """
        typer.echo("pong")

    return app


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> NoReturn:
    """Run the Forge CLI.

    This is the function referenced by ``[project.scripts]`` in
    ``pyproject.toml``. It is deliberately tiny: it builds the app,
    installs the exception handler, and calls the app.

    The function is declared ``-> NoReturn`` because it is a process
    entry point: every path through ``main()`` ends in a call to
    ``sys.exit()`` (via ``SystemExit`` or ``_handle_exception``),
    never in a normal return. This matches the contract of a real
    process entry point — the caller is the shell, and the shell
    expects an exit code, not a return value.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).
            Exposed for tests that want to invoke the CLI without
            touching ``sys.argv``.

    Raises:
        NoReturn: This function always exits, via ``SystemExit`` for
            clean exits (``--help``, ``--version``, successful
            commands) or via ``_handle_exception`` for unexpected
            errors.
    """
    app = create_app()

    # Resolve argv once so both the app call and the --debug detection
    # use the same list. This matters when a test passes ``argv``
    # explicitly: the exception handler must inspect the same arguments
    # the app received, not ``sys.argv``.
    actual_argv = argv if argv is not None else sys.argv[1:]

    # Detect --debug up front. The root callback also parses --debug and
    # stores it on ``ctx.obj``, but if a subcommand raises before the
    # callback runs (or if the callback itself raises), ``ctx.obj`` is
    # not available. Reading ``actual_argv`` directly is the safest way
    # to decide whether the contributor wants a traceback.
    debug = "--debug" in actual_argv

    try:
        # ``standalone_mode=False`` lets Typer return the command's
        # result instead of calling ``sys.exit()`` itself. That is the
        # right behaviour here: it lets us convert the return into an
        # explicit exit below, keeping the ``NoReturn`` contract honest
        # and preserving control over the process exit code.
        app(args=actual_argv, standalone_mode=False)
    except SystemExit:
        # Clean exit from Typer (--help, --version, typer.Exit).
        # Re-raise so the process exits with the correct code.
        raise
    except BaseException as exc:
        # Unhandled exception. ``_handle_exception`` always raises
        # ``typer.Exit`` with the appropriate code, so this path is
        # guaranteed to exit as well.
        _handle_exception(exc, debug=debug)

    # If ``app(...)`` returned normally, the command succeeded. Convert
    # that success into an explicit exit so every path through
    # ``main()`` ends in an exit rather than a return.
    #
    # Use ``SystemExit`` (the Python exit signal), *not* ``typer.Exit``.
    # ``typer.Exit`` is only meaningful inside a Typer command; raised
    # outside, it propagates as an uncaught exception and ``sys.exit()``
    # treats it as a failure (exit code 1 plus a traceback on stderr).
    # ``SystemExit`` is the canonical Python exit signal and is handled
    # correctly by ``sys.exit(main())`` in the console script.
    raise SystemExit(ExitCode.SUCCESS)


# -----------------------------------------------------------------------------
# Exports
# -----------------------------------------------------------------------------
__all__ = ["main", "create_app", "console"]
