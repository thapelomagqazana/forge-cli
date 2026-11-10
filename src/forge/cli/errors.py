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

1. **Cancellations** (``KeyboardInterrupt``, ``click.Abort``,
   ``typer.Abort``, ``EOFError``, ``PromptCancelledError``). These
   are neither failures nor errors. Rendered as a single dimmed
   "Cancelled." line on stderr, exiting with ``ExitCode.USER_ERROR``.

2. **``ForgeError`` and its subclasses.** The user's fault (bad
   input, missing blueprint, filesystem conflict). Rendered as a
   single red line with an optional hint, exiting with the
   subclass's declared ``exit_code``.

3. **``click.ClickException`` and its subclasses** (``UsageError``,
   ``BadParameter``, ``Abort``, etc.). These are *also* the user's
   fault: Click raises them when the user's input does not match the
   command's contract. Rendered as a friendly message with Click's
   own exit code (2 for usage errors). The full Click traceback is
   *not* shown, because the message already says what is wrong.

4. **Unexpected exceptions.** These are bugs. Rendered as a generic
   "an unexpected internal error occurred" message with a link to
   the issue tracker, exiting with ``ExitCode.INTERNAL_ERROR``. The
   message never includes the exception's class, message, or
   traceback — those could leak internal details or hints about
   vulnerabilities.

See ``forge/core/exceptions.py`` for the domain error hierarchy, the
exit-code table, and the rationale for each subclass's exit code.
This module consumes that hierarchy; it does not redefine it.

Why cancellations are checked before ``ForgeError``
---------------------------------------------------
``PromptCancelledError`` is a ``UserInputError`` and therefore a
``ForgeError`` subclass. If the classifier tested ``ForgeError``
first, a cancelled prompt would be rendered as an error (red ``✗``
prefix, message on stderr as a failure) instead of as a
cancellation (dimmed "Cancelled."). Both would exit with code 1, so
the *contract* would not be violated — but the *UX* would be wrong,
and acceptance tests that assert on the rendering would pass or fail
depending on which branch happened to be tested first. Putting
cancellation first makes the rendering deterministic and preserves
the "cancellations are commentary, not errors" rule from the module
docstring.

Why both ``click.Abort`` and ``typer.Abort`` are in the set
-----------------------------------------------------------
Older Typer re-exported ``click.Abort`` as ``typer.Abort`` — they
were the same class, and code written against that assumption could
use either name interchangeably. Current Typer does **not**:
``typer.Abort`` is ``typer.exceptions.Abort``, a distinct class that
is *not* a subclass of ``click.Abort``. A handler that checks only
``click.Abort`` will therefore miss every cancellation that arrives
through a Typer prompt while still catching every cancellation that
arrives through a Click prompt — an asymmetry that is invisible in
tests that construct one class and visible only in tests that
construct the other.

Both names belong in the tuple. A future Typer that re-aliases them
(or splits them again) is also covered: ``isinstance(x, (A, B))``
tolerates ``A is B`` silently, unlike the ``A | B`` union form
which raises ``TypeError: isinstance() argument 2 cannot contain
duplicate types``. Do not "clean this up" by removing one name on
the theory that "they're the same class." They are not, and a future
contributor relying on that theory will ship the bug.

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
--------------------------------------
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
7. **Cancellation is checked before error.** A ``PromptCancelledError``
   is technically a ``ForgeError`` (via ``UserInputError``), but its
   *category* is cancellation. The classifier checks the more
   specific category first, so the rendering follows the intent, not
   just the inheritance tree.

Why this module imports its error types from ``core``
-----------------------------------------------------
``MissingRequiredFlagsError`` and ``PromptCancelledError`` are
defined in ``forge.core.exceptions``, alongside the rest of the
domain error hierarchy. They live there because:

    * The exit-code contract is a domain decision (ADR-0005), and a
      subclass's ``exit_code`` is part of that contract regardless
      of *which* interaction layer raised it.
    * Moving them here would force ``core`` to import ``cli`` to
      re-export them for callers that import from ``core`` today —
      a dependency inversion (leaf importing leaf-down) that is
      fragile and hard to reason about.
    * A future non-CLI entry point (a web form, a gRPC service)
      could legitimately raise ``MissingRequiredFlagsError`` if it
      chose to model "required fields not provided in a non-
      interactive request" that way. Keeping the type in ``core``
      preserves that option without an import cycle.

The handler *renders* these classes with special semantics (the
cancellation-first branch, the standard error branch) — but
rendering is a CLI concern, so the rendering lives here. The
*types* are domain; the *classifier* is CLI. This module owns the
classifier and re-exports the types for convenience, so a caller
using ``from forge.cli.errors import handle_exception,
MissingRequiredFlagsError`` gets both in one import.
"""

from __future__ import annotations

from typing import NoReturn

import click
import typer

from forge.cli.ui import print_cancelled, print_error, print_hint

# The domain error hierarchy, plus the two interaction-layer subclasses
# that live alongside it. See ``forge/core/exceptions.py`` for the
# table of exit codes and the rationale for each subclass. The two
# subclasses re-exported here are the ones ``handle_exception``
# treats as *cancellations* and *aggregated-missing-flags* — they
# appear in this module's imports because the classifier needs them,
# not because they are defined here.
from forge.core.exceptions import (
    ExitCode,
    ForgeError,
    MissingRequiredFlagsError,
    PromptCancelledError,
)

__all__ = [
    "ExitCode",
    "MissingRequiredFlagsError",
    "PromptCancelledError",
    "handle_exception",
]


# -----------------------------------------------------------------------------
# Cancellation set
# -----------------------------------------------------------------------------
# The set of exceptions that mean "the user asked us to stop." These
# are *commentary*, not errors: the user chose to cancel, so the
# renderer must not use the error style (red ✗), must not print a
# hint, and must write to stderr (so a cancelled JSON stream never
# receives "Cancelled." interleaved into its product output).
#
# Why a tuple and not a ``|`` union:
#   * ``isinstance(x, A | B)`` raises ``TypeError: isinstance()
#     argument 2 cannot contain duplicate types`` when ``A is B``.
#     ``isinstance(x, (A, B))`` silently de-duplicates. That matters
#     here because ``click.Abort`` and ``typer.Abort`` have been the
#     same class in some Typer releases and distinct classes in
#     others; the tuple form works in both worlds, the union form
#     works in only one.
#   * A future Typer that re-aliases the two names (or splits them
#     again) is covered with no edit to this tuple. The union would
#     need a conditional at import time to build the correct set
#     per version. The tuple is unconditionally correct.
#
# Why BOTH ``click.Abort`` and ``typer.Abort`` are listed:
#   * They are distinct classes in current Typer. ``typer.Abort`` is
#     ``typer.exceptions.Abort``, which is **not** a subclass of
#     ``click.Abort``. A handler that listed only ``click.Abort``
#     would miss every cancellation that arrived through a Typer
#     prompt — while still catching every one that arrived through a
#     Click prompt. That asymmetry is invisible to tests that
#     construct one class and visible only to tests that construct
#     the other.
#   * They have been the same class in older Typer. The tuple
#     tolerates that (``isinstance`` de-duplicates), so listing both
#     is safe even when they refer to the same class. Do not remove
#     one on the theory that "they're the same" — the theory has
#     already been wrong once, and re-wronging it is one dependency
#     bump away.
#
# ``EOFError`` is included because a user piping input
# (``echo "" | forge create``) hits EOF at the first read; that is
# a cancellation, not a bug. ``KeyboardInterrupt`` covers Ctrl+C at
# any non-Typer point. ``PromptCancelledError`` covers the wizard's
# own cancellation path (see its docstring in
# ``forge.core.exceptions`` for why it is a ``UserInputError`` but
# rendered as a cancellation here).
_CANCELLATIONS: tuple[type[BaseException], ...] = (
    PromptCancelledError,
    KeyboardInterrupt,
    click.Abort,
    typer.Abort,  # Distinct class in current Typer; listed for that reason.
    EOFError,
)


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

    - **Cancellation** — ``KeyboardInterrupt``, ``click.Abort``,
      ``typer.Abort``, ``EOFError``, or ``PromptCancelledError``.
      Rendered as "Cancelled." on stderr, exit 1. ``--debug`` does
      not change this.
    - **``ForgeError``** — user error, rendered friendly, exit code
      from the subclass. ``--debug`` does not change this.
      ``MissingRequiredFlagsError`` (a subclass) is rendered by the
      same branch; its distinct hint carries the remediation
      ("Pass the missing flags, or re-run in an interactive
      terminal."), so no separate branch is needed.
    - **``click.ClickException``** — user error, rendered friendly,
      exit code from Click (2 for usage errors). ``--debug`` does not
      change this.
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
    # Cancellations — checked FIRST, before the ForgeError branch.
    # -------------------------------------------------------------------------
    # Ordering is deliberate, not incidental. ``PromptCancelledError``
    # is a ``UserInputError`` subclass, so a naive ``isinstance(exc,
    # ForgeError)`` check placed earlier would swallow it and render
    # it as a red error with a ``✗`` prefix — visually wrong for a
    # cancellation, and semantically wrong: the user *chose* to stop,
    # they did not fail. Cancellation is commentary, and commentary
    # goes through ``print_cancelled`` (stderr, dimmed, no prefix).
    #
    # ``typer.Abort`` subclasses ``click.ClickException`` in current
    # Typer, so if this branch did not exist (or if ``typer.Abort``
    # were missing from ``_CANCELLATIONS``) it would fall through to
    # the ClickException branch below and be rendered as a user
    # error with exit code 1 — numerically the same as the
    # cancellation branch, but with an error-style message instead
    # of "Cancelled." The numeric coincidence is exactly what makes
    # this bug hard to notice without a test that asserts on the
    # rendered *message*, not just the exit code.
    if isinstance(exc, _CANCELLATIONS):
        print_cancelled()
        raise SystemExit(ExitCode.USER_ERROR) from None

    # -------------------------------------------------------------------------
    # ForgeError and subclasses — the user's fault, with an exit code.
    #
    # This branch handles ``MissingRequiredFlagsError`` (via its
    # ``UserInputError`` parent) with no special-casing: the subclass
    # already overrides ``hint`` with the "pass these flags" text,
    # and the message lists every missing flag. A dedicated branch
    # would be behaviourally identical and would be deleted by the
    # next cleanup, taking any rationale with it. The comment here
    # is the documentation for that case.
    #
    # ``SystemExit`` (not ``typer.Exit``) — the handler is called
    # from ``main()``, outside Typer's dispatch, so Typer is not
    # there to catch ``typer.Exit``. ``SystemExit`` is the
    # exception ``sys.exit`` expects, and it exits the process
    # cleanly with the correct code.
    # -------------------------------------------------------------------------
    if isinstance(exc, ForgeError):
        print_error(str(exc))
        if exc.hint:
            print_hint(exc.hint)
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
    # We do **not** wrap ``exc.exit_code`` in ``ExitCode(...)`` even
    # though ADR-0013 discourages magic numbers. Click's usage error
    # is 2, which numerically equals ``ExitCode.BLUEPRINT_ERROR`` —
    # wrapping would *silently* relabel a usage error as a blueprint
    # error, which is the wrong signal for a CI consumer. Leave
    # Click's code raw and treat "Click owns its own code" as an
    # explicit exception to ADR-0013, documented here.
    #
    # ``click.exceptions.Exit`` is a subclass of ``ClickException``
    # but it is Click's *success* path (the user asked for help or
    # version). Typer catches it before the handler sees it, so the
    # handler does not need to special-case it here.
    if isinstance(exc, click.ClickException):
        print_error(exc.format_message())
        raise SystemExit(exc.exit_code) from None

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
        # ``raise exc`` outside an ``except`` context preserves the
        # exception's own ``__traceback__`` and appends one frame at
        # the ``raise`` — the developer sees the full original
        # traceback, including the raise site, plus a marker for
        # where the handler surfaced it. That is exactly what we
        # want.
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
__all__ = [
    "ExitCode",
    "MissingRequiredFlagsError",
    "PromptCancelledError",
    "handle_exception",
]
