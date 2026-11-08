"""Forge CLI entry point.

This module is the single, canonical entry point for the ``forge``
command. It is intentionally minimal: it creates the Typer application,
registers the root callback (for global options), delegates command
registration to the command registry, delegates error rendering to the
centralized handler, and hands control to Typer. It contains no
business logic, no knowledge of which commands exist, and no
error-rendering code of its own.

Design principles
-----------------
1. **Thin entry point.** ``main()`` does three things: create the app,
   delegate registration, and call the app. It does not print, and it
   does not render errors. Anything else belongs in a module under
   ``forge.cli`` or ``forge.core``.

2. **Version from metadata.** ``--version`` delegates to
   ``forge.core.version.get_version``, which reads the installed
   package version and falls back through a documented chain.

3. **Exit codes are contractual.** Every documented failure mode maps
   to an ``ExitCode`` enum value in ``forge.core.exceptions``. This
   module never invents a code; it converts between the two forms the
   process boundary understands (``typer.Exit`` from command bodies,
   ``SystemExit`` from the entry point).

4. **Error handling is delegated.** Unhandled exceptions are rendered
   by ``forge.cli.errors.handle_exception``, which is tested in
   isolation. This module hands the exception off; it does not decide
   what the user sees.

5. **Output is delegated.** Version output goes through
   ``forge.cli.ui.print_info``, which routes to stdout. This module
   does not touch a console.

6. **Command-agnostic.** The entry point does not import, list, or
   reference any specific command. Adding a command means adding a
   module under ``forge.cli.commands`` — this file never changes.

7. **Testable.** The app can be invoked via ``Typer.CliRunner``
   without launching a subprocess. The exception handler is tested
   separately (``tests/unit/cli/test_errors.py``), and the output
   helpers are tested separately (``tests/unit/cli/test_ui.py``).

Non-obvious decisions
---------------------
* **``standalone_mode=False`` on ``app(...)``.** By default Typer
   converts its ``typer.Exit`` exceptions into ``sys.exit()`` calls
   and its ``UsageError`` exceptions into rendered messages followed
   by ``sys.exit(2)``. ``standalone_mode=False`` disables both
   conversions, leaving the raw exceptions for ``main()`` to
   classify. That is the whole point of the flag: the entry point
   owns the exit-code contract, not Typer.

* **Three exception types are "clean exits", not crashes.**
   ``main()`` distinguishes them explicitly:

   1. ``typer.Exit`` — a command body signalling "I finished with
      this code". Converted to ``SystemExit(code)``.
   2. ``SystemExit`` — already the canonical exit signal. Propagated
      unchanged.
   3. Usage errors (``UsageError`` and its subclasses) — Typer
      rejecting the invocation for a malformed argument. Converted
      to ``SystemExit(2)``.

   Everything else is a genuine unexpected exception and goes to
   ``handle_exception``, which renders the generic "unexpected
   internal error" and exits 5. That split is the exit-code contract
   in code: 0/1/2 are user-facing, 5 is "report a bug".

* **Usage errors are classified by *class name*, not by class
   identity.** Typer vendors a private copy of Click under
   ``typer._click``. Its ``UsageError`` is *not* the same class as
   ``click.exceptions.UsageError`` — the two have separate
   inheritance trees. An ``isinstance`` check against either one
   misses the other. The class *names* are the stable API (Typer
   mirrors Click's public names intentionally); the module paths and
   class identities are not. See ``_USAGE_ERROR_CLASS_NAMES`` for the
   list and the reasoning.

* **``SystemExit``, not ``typer.Exit``, at the bottom of ``main()``.**
   ``typer.Exit`` is meaningful only *inside* a Typer command; raised
   outside, it propagates as an uncaught exception, and ``sys.exit()``
   treats an uncaught exception as failure (exit code 1 plus a
   traceback). ``SystemExit`` is the canonical Python exit signal and
   is handled correctly by ``sys.exit(main())`` in the console script.

* **Two ``--debug`` detections.** The root callback parses
   ``--debug`` and stores it on ``ctx.obj`` so subcommands can read
   it. The exception handler, however, may run *before* the callback
   (e.g. if the callback itself raises, or if Typer rejects the
   invocation before invoking the callback). For that path,
   ``_resolve_debug_flag(argv)`` scans ``argv`` directly. The two
   sources are intentionally redundant: ``ctx.obj["debug"]`` is the
   truth for subcommands, ``_resolve_debug_flag(argv)`` is the
   superset the exception handler uses, and the two agree whenever
   the callback ran. ``_resolve_debug_flag`` is documented as a
   fallback, not as a competing parser.

* **Strict command registration in production.** A command module
   that does not expose a module-level ``app: typer.Typer`` is a
   *packaging bug*, not a user-runtime condition. Skipping it
   silently would mean a user sees fewer commands than the release
   notes promised, with no diagnostic. Failing at startup means the
   bug is caught the first time the wheel is installed, on a
   contributor's machine, not in a user's terminal six weeks later.
"""

from __future__ import annotations

import sys
from typing import Annotated, Final, NoReturn

import typer

from forge.cli.errors import handle_exception
from forge.cli.registry import register_commands
from forge.cli.ui import print_info
from forge.core.exceptions import ExitCode
from forge.core.version import get_version

# -----------------------------------------------------------------------------
# Usage-error class names
# -----------------------------------------------------------------------------
# The set of exception class *names* that represent a malformed
# invocation — a user mistake, not a CLI bug. Matched by name rather
# than by ``isinstance`` because Typer vendors a private copy of
# Click under ``typer._click``, and its ``UsageError`` is a *different
# class* from ``click.exceptions.UsageError``. An ``isinstance``
# check against either one silently misses the other, and the
# fallthrough is exit 5 (internal error) — the exact wrong
# classification.
#
# The names below are Click's public API. They have been stable for
# years, and both Click and Typer have committed to them in their
# documentation. A rename would be a breaking change to those
# libraries' public surface, which neither has any reason to make.
#
# If a future Typer or Click adds a new subclass that represents an
# invocation error, it will not be in this set — the exception will
# fall through to ``handle_exception`` (exit 5) with the standard
# "report a bug" message. That is the correct conservative fallback:
# a novel exception shape should not be silently misclassified as a
# usage error just because it *might* be one. The user sees the exit
# 5 message with a clear note to report the issue, and the
# maintainers see it in the issue tracker and add the name to this
# set.
_USAGE_ERROR_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "UsageError",  # click.exceptions.UsageError
        "BadParameter",  # click.exceptions.BadParameter — invalid value
        "NoSuchOption",  # click.exceptions.NoSuchOption — unknown --flag
        "MissingParameter",  # click.exceptions.MissingParameter — required not provided
        "BadOptionUsage",  # click.exceptions.BadOptionUsage — option used wrong
    }
)


# -----------------------------------------------------------------------------
# Version callback
# -----------------------------------------------------------------------------
def _version_callback(value: bool) -> None:
    """Print the resolved version and exit, if ``--version`` was passed.

    Called eagerly by Typer *before* any subcommand runs, so that
    ``forge --version`` works even if the subcommand is invalid.

    Delegates to ``forge.core.version.get_version``, which:
      - Reads from installed package metadata (``importlib.metadata``).
      - Falls back to the bundled ``_version.py`` (generated by
        ``hatch-vcs`` at build time).
      - Falls back to the ``FORGE_VERSION`` environment variable.
      - Falls back to ``"0.0.0.dev0"``.

    The resolved string is cached for the lifetime of the process, so
    ``forge --version`` reports the same value no matter how many times
    it is called during a single invocation.

    Output goes through ``forge.cli.ui.print_info``, which writes to
    stdout. This module does not own a console; the voice belongs to
    the ``ui`` module.
    """
    if value:
        print_info(f"forge, version {get_version()}")
        raise typer.Exit(code=ExitCode.SUCCESS)


# -----------------------------------------------------------------------------
# Debug-flag detection
# -----------------------------------------------------------------------------
def _resolve_debug_flag(argv: list[str]) -> bool:
    """Return ``True`` if ``--debug`` appears anywhere in ``argv``.

    This is intentionally a simple ``in`` check rather than a full
    argument parser. See the module docstring's "Two ``--debug``
    detections" note for the invariant this function maintains and
    the reason a full parse would be worse:

    1. A full parse here would duplicate Typer's parsing logic, and
       the two could disagree. A literal string check cannot disagree
       with itself.
    2. The only way ``--debug`` can appear in ``argv`` *without* being
       the debug flag is if a subcommand declares its own ``--debug``
       option. No Forge command does, and if one ever does, this
       helper should be revisited — the test suite asserts the
       absence by scanning every registered command's options.

    Args:
        argv: The argument list passed to ``main()`` (or
            ``sys.argv[1:]`` if none was passed).

    Returns:
        ``True`` if ``--debug`` is in ``argv``, ``False`` otherwise.
    """
    return "--debug" in argv


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

    The ``ctx.obj`` dict is the truth for subcommands; the exception
    handler additionally scans ``argv`` (see
    ``_resolve_debug_flag``) because it may run before this callback.
    Whenever this callback runs, the two sources agree.
    """
    ctx.obj = {"debug": debug}


# -----------------------------------------------------------------------------
# Application factory
# -----------------------------------------------------------------------------
def create_app() -> typer.Typer:
    """Build and return the root Typer application.

    This is a *factory*, not a module-level constant. That makes the app
    re-creatable for tests and for future multi-app scenarios (e.g., a
    separate ``forge-admin`` CLI that reuses the same commands).

    Command registration is delegated to ``forge.cli.registry``. This
    factory does not know which commands exist — it only knows how to
    ask the registry to discover and register them. Adding a new
    command does not require editing this file.

    Returns:
        A fully-configured ``typer.Typer`` instance with all commands
        mounted and the root callback installed.

    Raises:
        RuntimeError: If a command module violates the registry
            contract (missing ``app`` attribute, wrong type). Failing
            at startup surfaces packaging bugs immediately rather
            than shipping a wheel with silently missing commands.
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
        callback=_root_callback,
        no_args_is_help=True,
        add_completion=True,
        rich_markup_mode=None,
        context_settings={"help_option_names": ["-h", "--help"]},
    )

    # -------------------------------------------------------------------------
    # Command registration
    # -------------------------------------------------------------------------
    # ``strict=True`` is deliberate: a malformed command module is a
    # packaging bug, not a user-runtime condition. Failing here means
    # the wheel was broken at install time, not at the user's first
    # invocation. If a future release needs to tolerate a plugin
    # ecosystem with best-effort discovery, that is a *different*
    # registry call (``register_plugins``), not a loosening of the
    # core contract.
    register_commands(app, strict=True)

    return app


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> NoReturn:
    """Run the Forge CLI.

    This is the function referenced by ``[project.scripts]`` in
    ``pyproject.toml``. It is deliberately tiny: it builds the app,
    hands uncaught exceptions to the centralized handler, and calls
    the app. It does not print, and it does not render errors.

    The function is declared ``-> NoReturn`` because it is a process
    entry point: every path through ``main()`` ends in a call to
    ``sys.exit()`` (via ``SystemExit`` or ``handle_exception``),
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
            commands, usage errors) or via ``handle_exception`` for
            unexpected errors.
    """
    app = create_app()

    # Resolve argv once so both the app call and the --debug detection
    # use the same list. This matters when a test passes ``argv``
    # explicitly: the exception handler must inspect the same arguments
    # the app received, not ``sys.argv``.
    actual_argv = argv if argv is not None else sys.argv[1:]

    # Detect --debug up front. The root callback also parses --debug
    # and stores it on ``ctx.obj``, but if a subcommand raises before
    # the callback runs (or if the callback itself raises), ``ctx.obj``
    # is not available. Reading ``actual_argv`` directly is the safest
    # way to decide whether the contributor wants a traceback.
    debug = _resolve_debug_flag(actual_argv)

    # ------------------------------------------------------------------
    # Exit-code classification: three clean-exit types, one crash type
    # ------------------------------------------------------------------
    #
    # The order of the ``except`` clauses below is significant, because
    # Python matches them top-to-bottom and the classes overlap:
    #
    #   * ``typer.Exit`` is a subclass of ``Exception`` (not
    #     ``SystemExit``). It must be caught before ``BaseException``.
    #   * ``SystemExit`` is a subclass of ``BaseException`` (not
    #     ``Exception``). It must be caught before ``BaseException``
    #     to propagate unchanged.
    #   * Usage errors (by class name — see ``_USAGE_ERROR_CLASS_NAMES``)
    #     are the classes Typer raises for malformed invocations. They
    #     are caught in the ``BaseException`` branch by name because
    #     ``isinstance`` cannot match Typer's vendored Click classes
    #     against ``click.exceptions``.
    #
    # The catch order below is the canonical order:
    #
    #   1. ``typer.Exit`` — command body signalled "I finished with
    #      this code". Convert to ``SystemExit(code)``.
    #   2. ``SystemExit`` — already the canonical exit signal.
    #      Propagate unchanged.
    #   3. Anything else — check the class name against
    #      ``_USAGE_ERROR_CLASS_NAMES``; if it matches, convert to
    #      ``SystemExit(2)``. Otherwise hand to ``handle_exception``,
    #      which classifies it as an internal error (exit 5).
    #
    # The ``typer.Exit`` clause must come *before* the
    # ``_USAGE_ERROR_CLASS_NAMES`` check because ``typer.Exit`` is a
    # clean exit regardless of whether its class name happens to
    # match a usage-error name (it does not today, but the ordering
    # is defensive).
    try:
        result = app(args=actual_argv, standalone_mode=False)
    except typer.Exit as exc:
        # Command body signalled "I finished with this code".
        # Convert to ``SystemExit`` — the canonical exit signal — so
        # the shell sees ``$? == exc.exit_code``.
        raise SystemExit(exc.exit_code) from exc
    except SystemExit:
        # Already the canonical exit signal. Propagate unchanged.
        raise
    except BaseException as exc:
        # Typer raises its own vendored copies of Click's exception
        # classes when the invocation is malformed. Those copies live
        # under ``typer._click.exceptions`` and are *not* identical to
        # ``click.exceptions.*`` — ``isinstance(exc, click.UsageError)``
        # returns False for a Typer-vendored ``UsageError`` because the
        # two classes have separate inheritance trees. The class
        # *names* are the stable API (Typer mirrors Click's public
        # names intentionally); the module paths are not.
        #
        # ``type(exc).__name__`` works for both the vendored and the
        # standalone-imported classes, and for any future Typer
        # release that switches from one to the other.
        if type(exc).__name__ in _USAGE_ERROR_CLASS_NAMES:
            raise SystemExit(ExitCode.USER_ERROR) from exc
        # Not a clean exit. Delegate to the central handler, which
        # renders the generic "unexpected internal error" message and
        # exits with ``ExitCode.INTERNAL_ERROR`` — or re-raises under
        # ``--debug`` for a full traceback.
        handle_exception(exc, debug=debug)

    # ``app(...)`` returned normally. Interpret the return value as
    # the command's exit code, or as "the command succeeded".
    #
    # ``standalone_mode=False`` always returns the callback's return
    # value from ``app(...)``. That is the documented contract, and
    # it is the mechanism the project's commands use to signal their
    # exit code (see ``validate.py``'s "Exit-code mechanism"
    # docstring). The return value is:
    #
    #   * an ``int`` — the command's exit code (a command declared
    #     ``-> int`` that returned successfully).
    #   * ``None`` — the command's callback returned normally without
    #     signalling a code (a command declared ``-> None``, or a
    #     command declared ``-> int`` that fell off the end after an
    #     unreachable branch).
    #
    # Both cases are treated as success unless the value is a
    # non-zero integer.
    if isinstance(result, int):
        raise SystemExit(result)
    raise SystemExit(ExitCode.SUCCESS)


# -----------------------------------------------------------------------------
# Exports
# -----------------------------------------------------------------------------
__all__ = ["create_app", "main"]
