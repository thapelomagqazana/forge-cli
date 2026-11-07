"""The centralized exception handler.

Every uncaught exception in Forge CLI passes through
``handle_exception``. It is the single place where a failure becomes
a user-facing message and an exit code. The handler is deliberately
isolated from ``main.py`` so it can be tested in isolation — testing
the handler through ``CliRunner`` is unreliable, because Typer
intercepts exceptions before they reach the handler.

The handler is a **classifier**, not a single renderer. It identifies
the exception's category first, then dispatches to the right
renderer. The categories are:

1. **``ForgeError`` and its subclasses.** The user's fault (bad
   input, missing blueprint, filesystem conflict). Rendered as a
   single red line with an optional hint, exiting with the
   subclass's declared ``exit_code``.

2. **``click.ClickException`` and its subclasses** (``UsageError``,
   ``BadParameter``, ``Abort``, etc.). These are *also* the user's
   fault: Click raises them when the user's input does not match the
   command's contract. Rendered as a friendly message with Click's
   own exit code (2 for usage errors). The full Click traceback is
   *not* shown, because the message already says what is wrong.

3. **Cancellations** (``KeyboardInterrupt``, ``typer.Abort``). These
   are neither failures nor errors. Rendered as a single dimmed
   "Cancelled." line on stderr, exiting with ``ExitCode.USER_ERROR``.

4. **Unexpected exceptions.** These are bugs. Rendered as a generic
   "an unexpected internal error occurred" message with a link to
   the issue tracker, exiting with ``ExitCode.INTERNAL_ERROR``. The
   message never includes the exception's class, message, or
   traceback — those could leak internal details or hints about
   vulnerabilities.

Why ``--debug`` does *not* re-raise user errors
-----------------------------------------------
``--debug`` is a tool for the *developer of Forge*, not for the
*user of Forge*. Its contract is: "if an *unexpected* exception
occurs, show me the traceback so I can debug it." An unexpected
exception is a bug in Forge — something the developer did not
anticipate.

A ``UsageError`` is *not* a bug. The user typed a command that does
not exist, or passed an argument that does not match the signature.
There is nothing to debug. Showing the full Typer internals is
noise, not signal.

The correct behaviour is: even under ``--debug``, a *user* error
(``ForgeError``, ``ClickException``) is rendered as a friendly
message. ``--debug`` only affects *unexpected* exceptions. This is
the standard contract for every serious CLI: ``--debug`` is for the
developer, not for the user's typos.

Why ``SystemExit``, not ``typer.Exit``
---------------------------------------
The handler is called from ``main()``, *after* Typer's dispatch has
finished. ``typer.Exit`` is only special *inside* a Typer command —
Typer catches it there and converts it to a ``SystemExit`` with the
right code. Outside Typer, ``typer.Exit`` is just an ordinary
exception: it propagates into ``sys.exit(main())``, which treats it
as an uncaught exception, prints a traceback, and exits 1.

The fix is to raise the exception ``sys.exit`` actually expects:
``SystemExit``. It is the same exit signal Typer would have raised,
but raised directly by the handler. The effect is identical: the
process exits with the correct code, no traceback.

Why no stack traces for users
-----------------------------
A stack trace is a developer message, not a user message. The user
does not know what "File main.py, line 47, in create_project" means.
They just know: "it broke." A one-line message with a hint is more
useful to them than a 20-line traceback. The developer who needs
the traceback passes ``--debug``. Two audiences, two messages, one
handler.

Why cancellations go to stderr
------------------------------
A cancellation (Ctrl+C, ``typer.Abort``, a user declining a prompt)
is neither the product of the command nor an error. It is
*commentary* — the user told the command to stop, and the command is
acknowledging it. The Unix contract puts commentary on stderr, so a
script that pipes ``forge verify --json`` to ``jq`` does not receive
"Cancelled." mixed into its JSON when the user presses Ctrl+C.

This distinction is what the ``print_cancelled`` helper enforces.
The handler uses it (not ``print_info``) for the cancellation
branch, so the message cannot accidentally land on stdout.

Design principles
-----------------
1. **Fail loudly in development, quietly in production.** The
   default is the quiet message; ``--debug`` is the loud traceback —
   but only for *unexpected* exceptions. User errors and
   cancellations are always rendered quietly.
2. **Never leak internal details to the user.** The unexpected
   exception handler does not include the exception's class name or
   message. Those go to ``--debug`` only.
3. **Never swallow an exception silently.** Every path through the
   handler raises ``SystemExit`` (or re-raises under ``--debug``).
   There is no "return None" path.
4. **The exit code is the contract.** Every ``ForgeError`` subclass
   and every ``ClickException`` has a declared exit code. The
   handler uses it. Tests assert on it.
5. **The stream is the contract.** Product goes to stdout, commentary
   to stderr. The handler uses the helper that matches the message's
   category: ``print_error`` for errors, ``print_hint`` for hints,
   ``print_cancelled`` for cancellations. It does not repurpose one
   helper for another category.
6. **Classify before rendering.** The category of the exception
   determines its rendering, its exit code, and its ``--debug``
   behaviour. The handler is a dispatcher, not a single renderer.
"""

from __future__ import annotations

from typing import NoReturn

import click
import typer

from forge.cli.ui import print_cancelled, print_error, print_hint
from forge.core.exceptions import ExitCode, ForgeError


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
def handle_exception(exc: BaseException, *, debug: bool) -> NoReturn:
    """Render an exception to the user and exit with the correct code.

    This is the single entry point for every uncaught exception in the
    CLI. It is called from ``main()`` after the app raises. It is also
    called from tests directly, to assert on the rendering without
    going through the CLI.

    The handler classifies the exception into one of four categories
    and dispatches to the matching renderer. The categories and their
    ``--debug`` behaviour:

    - **``ForgeError``** — user error, rendered friendly, exit code
      from the subclass. ``--debug`` does not change this.
    - **``click.ClickException``** — user error, rendered friendly,
      exit code from Click (2 for usage errors). ``--debug`` does not
      change this.
    - **Cancellation** — ``KeyboardInterrupt`` / ``typer.Abort``,
      rendered as "Cancelled." on stderr, exit 1. ``--debug`` does
      not change this.
    - **Unexpected exception** — bug, rendered as a generic message,
      exit 5. ``--debug`` *re-raises* the exception, so the developer
      sees the traceback.

    Args:
        exc: The exception to render.
        debug: If ``True``, re-raise *unexpected* exceptions so the
            developer sees the traceback. User errors and cancellations
            are still rendered friendly.

    Raises:
        NoReturn: This function always exits, either by re-raising an
            unexpected exception (under ``--debug``) or by raising
            ``SystemExit`` with the appropriate code.
    """
    # -------------------------------------------------------------------------
    # ForgeError and subclasses — the user's fault, with an exit code
    # -------------------------------------------------------------------------
    if isinstance(exc, ForgeError):
        print_error(str(exc))
        if exc.hint:
            print_hint(exc.hint)
        # ``SystemExit`` (not ``typer.Exit``) — the handler is called
        # from ``main()``, outside Typer's dispatch, so Typer is not
        # there to catch ``typer.Exit``. ``SystemExit`` is the
        # exception ``sys.exit`` expects, and it exits the process
        # cleanly with the correct code.
        raise SystemExit(exc.exit_code) from None

    # -------------------------------------------------------------------------
    # Click exceptions — the user's fault, but from Click's parser
    # -------------------------------------------------------------------------
    # ``click.ClickException`` is the parent of ``UsageError`` (the
    # user typed a command that does not exist, or passed an argument
    # that does not match the signature), ``BadParameter``, and
    # ``Abort`` (the user declined a prompt). These are user errors,
    # not bugs. Render them as friendly messages with Click's own
    # exit code (2 for usage errors), not as "unexpected internal
    # error" with exit 5.
    #
    # ``click.exceptions.Exit`` is a subclass of ``ClickException``
    # but it is Click's *success* path (the user asked for help or
    # version). Typer catches it before the handler sees it, so the
    # handler does not need to special-case it here.
    if isinstance(exc, click.ClickException):
        print_error(exc.format_message())
        raise SystemExit(exc.exit_code) from None

    # -------------------------------------------------------------------------
    # Cancellations — not failures, just "the user stopped"
    # -------------------------------------------------------------------------
    if isinstance(exc, KeyboardInterrupt | typer.Abort):
        # A single dimmed line on stderr. No traceback, no "Error:"
        # prefix. The exit code is non-zero so scripts know the
        # command did not complete.
        #
        # This goes through ``ui.print_cancelled``, which writes to
        # *stderr* — the message is commentary, not product. Using
        # ``print_info`` here would put "Cancelled." on stdout, which
        # would break any script parsing ``forge verify --json`` that
        # the user interrupts with Ctrl+C.
        print_cancelled()
        raise SystemExit(ExitCode.USER_ERROR) from None

    # -------------------------------------------------------------------------
    # Unexpected exceptions — bugs, with no internal details leaked
    # -------------------------------------------------------------------------
    # From here on, the exception is a bug in Forge. Under ``--debug``,
    # re-raise it so the developer sees the traceback. Without
    # ``--debug``, render a generic message and point at the issue
    # tracker.
    #
    # Do NOT include ``type(exc).__name__`` or ``str(exc)`` in the
    # rendered message. Those are internal details that could hint at
    # a vulnerability. The user gets a generic message and a link to
    # the issue tracker; the developer gets the traceback via --debug.
    if debug:
        raise exc

    print_error("An unexpected internal error occurred.")
    print_hint(
        "Run with --debug to see the full traceback, or report this at "
        "https://github.com/thapelomagqazana/forge-cli/issues"
    )
    raise SystemExit(ExitCode.INTERNAL_ERROR) from None


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
__all__ = ["handle_exception"]
