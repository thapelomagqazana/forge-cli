"""The centralized exception handler.

Every uncaught exception in Forge CLI passes through
``handle_exception``. It is the single place where a failure becomes a
user-facing message and an exit code. The handler is deliberately
isolated from ``main.py`` so it can be tested in isolation — testing
the handler through ``CliRunner`` is unreliable, because Typer
intercepts exceptions before they reach the handler.

The handler is a **classifier**, not a single renderer. It identifies
the exception's category first, then dispatches to the right renderer.
The categories are:

1. **Cancellations** (``KeyboardInterrupt``, ``click.Abort``,
   ``typer.Abort``, ``EOFError``, ``PromptCancelledError``). These
   are neither failures nor errors. Rendered as a single dimmed
   "Cancelled." line on stderr, exiting with ``ExitCode.USER_ERROR``.

2. **Safety errors** (``SafetyError`` and its ``CollisionError``
   subclass, both from ``forge.core.exceptions``). These carry a
   *list of findings*, not a single message. Rendered as a header
   line plus one indented line per finding, exiting with the
   subclass's declared code (3 for structural, 6 for collisions).
   ``--overwrite`` is offered *only* for ``CollisionError``, because
   structural safety failures cannot be fixed by the flag.

3. **``ForgeError`` and its subclasses** that carry a simple
   message. The user's fault (bad input, missing blueprint). Rendered
   as a single red line with an optional hint, exiting with the
   subclass's declared ``exit_code``.

4. **``click.ClickException``** (``UsageError``, ``BadParameter``,
   ``Abort``, etc.). Also the user's fault: Click raises them when
   the user's input does not match the command's contract. Rendered
   as a friendly message with Click's own exit code (2 for usage
   errors). The full Click traceback is *not* shown, because the
   message already says what is wrong.

5. **Unexpected exceptions.** Bugs. Rendered as a generic "an
   unexpected internal error occurred" message with a link to the
   issue tracker, exiting with ``ExitCode.INTERNAL_ERROR``. The
   message never includes the exception's class, message, or
   traceback — those could leak internal details or hints about
   vulnerabilities.

The exit code that does **not** pass through this handler
---------------------------------------------------------
``ExitCode.DRY_RUN_COLLISIONS`` (7) is produced by the ``create``
command's ``_run_dry_run`` function via a direct
``raise SystemExit(ExitCode.DRY_RUN_COLLISIONS)``, *after* the plan
has been rendered to stdout. The dry-run path does not raise an
exception in the ordinary sense — it produces a valid plan that
happens to contain collisions, and exits with a code that tells CI
"the plan is on stdout; parse it and decide."

By the time ``_run_dry_run`` raises, there is no *exception* to
classify: the dry-run **succeeded**. It would be a category error to
route this through ``handle_exception``, whose entire job is to
convert *failures* into messages. The handler correctly lets any
``SystemExit`` propagate (via ``main()``, which catches ``SystemExit``
and lets it pass through rather than sending it to this handler).

This is why the exit-code table in ``forge/core/exceptions.py`` has
no ``ForgeError`` subclass that resolves to 7: no exception *means*
"the dry-run found conflicts." The dry-run path knows that fact
directly and exits accordingly. See ``docs/reference/DRY_RUN_PLAN.md``
for the user-facing contract that documents exit 7.

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
the "cancellations are commentary, not errors" rule.

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

Why the safety-error branch comes before the generic ``ForgeError``
-------------------------------------------------------------------
``SafetyError`` and ``CollisionError`` are ``ForgeError`` subclasses,
but they carry a *findings list* that the generic branch does not
know how to render. If the generic branch ran first, the user would
see ``str(exc)`` — which is the composed message, including every
finding — as a single unwrapped red line. That is *functional* but
*unhelpful*: no per-finding colouring, no per-finding hint
indentation, no offer of ``--overwrite`` for the collision case.

Putting the safety branch first lets the handler render findings
with the presentation helpers the rest of the CLI uses
(``print_error`` for the header, ``print_error_indented`` for
findings, ``print_hint`` for hints), matching the visual language of
the loading and planning output. A user who has seen one Forge
error recognises every Forge error.

Why ``--overwrite`` is offered only for ``CollisionError``
----------------------------------------------------------
The flag consents to replacing existing files. That consent is
*only* relevant to collisions. A traversal attempt, an absolute
path, a symlink escape, an unwritable parent — none of these become
safer with ``--overwrite``; the flag would have no effect. Offering
it would teach users to pass it reflexively, which defeats its
purpose.

The two error types are distinguished at the *type* level
(``CollisionError`` vs. ``SafetyError``), not by inspecting a message
or a finding code — the exit-code contract (ADR-0017) already
distinguishes them, and the type hierarchy mirrors that distinction.
The handler branches on the type, so an ordinary ``SafetyError``
never sees the ``--overwrite`` hint.

Why ``--debug`` does *not* re-raise user errors
-----------------------------------------------
``--debug`` is a tool for the *developer of Forge*, not for the
*user of Forge*. Its contract is: "if an *unexpected* exception
occurs, show me the traceback so I can debug it." An unexpected
exception is a bug in Forge — something the developer did not
anticipate.

A ``UsageError`` is *not* a bug. A ``SafetyError`` is *not* a bug.
The user typed a command that does not exist, or a blueprint that
points outside the root. There is nothing to debug. Showing the
full internals is noise, not signal.

The correct behaviour is: even under ``--debug``, a *user* error
(``ForgeError``, ``ClickException``) is rendered as a friendly
message. ``--debug`` only affects *unexpected* exceptions.

Why ``SystemExit``, not ``typer.Exit``
--------------------------------------
The handler is called from ``main()``, *after* Typer's dispatch has
finished. ``typer.Exit`` is only special *inside* a Typer command —
Typer catches it there and converts it to a ``SystemExit`` with the
right code. Outside Typer, ``typer.Exit`` is just an ordinary
exception: it propagates into ``sys.exit(main())``, which treats it
as an uncaught exception, prints a traceback, and exits 1.

The fix is to raise the exception ``sys.exit`` actually expects:
``SystemExit``.

Why no stack traces for users
-----------------------------
A stack trace is a developer message. The user gets a one-line
message and a hint. The developer passes ``--debug``. Two audiences,
two messages, one handler.

Why cancellations go to stderr
------------------------------
A cancellation (Ctrl+C, ``typer.Abort``, a user declining a prompt)
is neither the product of the command nor an error. It is
*commentary*. The Unix contract puts commentary on stderr, so a
script that pipes ``forge verify --json`` to ``jq`` does not receive
"Cancelled." mixed into its JSON when the user presses Ctrl+C.

Design principles
-----------------
1. **Fail loudly in development, quietly in production.** The
   default is the quiet message; ``--debug`` is the loud traceback —
   but only for *unexpected* exceptions.
2. **Never leak internal details to the user.** The unexpected
   exception handler does not include the exception's class name or
   message. Those go to ``--debug`` only.
3. **Never swallow an exception silently.** Every path through the
   handler raises ``SystemExit`` (or re-raises under ``--debug``).
4. **The exit code is the contract.** Every ``ForgeError`` subclass
   and every ``ClickException`` has a declared exit code.
5. **The stream is the contract.** Product goes to stdout, commentary
   to stderr.
6. **Classify before rendering.** The category of the exception
   determines its rendering, its exit code, and its ``--debug``
   behaviour.
7. **Cancellation is checked before error.** A ``PromptCancelledError``
   is technically a ``ForgeError``, but its *category* is
   cancellation. The classifier checks the more specific category
   first, so the rendering follows the intent, not the inheritance
   tree.
8. **Safety is checked before generic ForgeError.** A ``SafetyError``
   is technically a ``ForgeError``, but its *category* is safety —
   it carries findings that need their own rendering. The classifier
   checks the more specific category first, so the presentation
   matches the data shape.
9. **Not everything with an exit code is an exception.** A dry-run
   that finds collisions *succeeds* — the plan is produced, the user
   has information — and exits with a code that says so. That code
   (7) never reaches this handler, and that is by design: the handler
   classifies *failures*, and a dry-run's collision report is not one.
"""

from __future__ import annotations

from typing import NoReturn

import click
import typer

from forge.cli.ui import (
    print_cancelled,
    print_error,
    print_error_indented,
    print_hint,
)
from forge.core.exceptions import (
    CollisionError,
    ExitCode,
    ForgeError,
    PromptCancelledError,
    SafetyError,
)

__all__ = [
    "ExitCode",
    "handle_exception",
]


# -----------------------------------------------------------------------------
# Cancellation set
# -----------------------------------------------------------------------------
# See the module docstring for the two Typer/Click coexistence reasons.
# ``EOFError`` is included because a user piping input
# (``echo "" | forge create``) hits EOF at the first read — that is a
# cancellation, not a bug.
_CANCELLATIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    click.Abort,
    typer.Abort,
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

    The handler classifies the exception into one of five categories
    and dispatches to the matching renderer. The categories and their
    ``--debug`` behaviour:

    - **Cancellation** — ``KeyboardInterrupt``, ``click.Abort``,
      ``typer.Abort``, ``EOFError``, or ``PromptCancelledError``.
      Rendered as "Cancelled." on stderr, exit 1.
    - **Safety** — ``SafetyError`` or ``CollisionError``. Rendered
      with findings, one per line. ``--overwrite`` offered only for
      ``CollisionError``. Exit code from the subclass (3 or 6).
    - **``ForgeError``** — user error, rendered friendly, exit code
      from the subclass.
    - **``click.ClickException``** — user error, rendered friendly,
      exit code from Click (2 for usage errors).
    - **Unexpected exception** — bug, rendered as a generic message,
      exit 5. Under ``--debug``, re-raises so the developer sees the
      traceback.

    ``--debug`` does not change the rendering of user errors or
    cancellations, because they are not bugs and there is nothing to
    debug.

    Not a category handled here: ``ExitCode.DRY_RUN_COLLISIONS`` (7).
    The dry-run path raises ``SystemExit(7)`` directly from
    ``create._run_dry_run`` *after* rendering the plan — there is no
    exception to classify. ``SystemExit`` from that path flows
    through ``main()``, which lets it pass through unmodified. This
    handler is for failures; the dry-run's collision report is not
    one. See the module docstring's "The exit code that does **not**
    pass through this handler" section.

    Raises:
        NoReturn: always exits, either by re-raising (under
            ``--debug``, unexpected only) or by raising ``SystemExit``.
    """
    # -------------------------------------------------------------------------
    # 1. Cancellations — checked FIRST (see module docstring).
    # -------------------------------------------------------------------------
    # ``PromptCancelledError`` is a ``UserInputError`` (and therefore a
    # ``ForgeError``), so it must be tested here — before the generic
    # ``ForgeError`` branch. Otherwise the user sees a red error line
    # for what is actually a clean cancellation. See module docstring,
    # "Why cancellations are checked before ``ForgeError``."
    #
    # ``PromptCancelledError`` is imported at module level (not inside
    # this function) because it is a *first-class* member of the
    # cancellation set — not an afterthought — and the import belongs
    # where a reviewer scanning the module's top will see it.
    if isinstance(exc, (PromptCancelledError, *_CANCELLATIONS)):
        print_cancelled()
        raise SystemExit(ExitCode.USER_ERROR) from None

    # -------------------------------------------------------------------------
    # 2. Safety errors — a distinct category with its own renderer.
    # -------------------------------------------------------------------------
    # Checked before the generic ``ForgeError`` branch so findings get
    # the presentation the rest of the CLI uses. See module docstring,
    # "Why the safety-error branch comes before the generic ForgeError."
    #
    # ``CollisionError`` is checked first because it is a ``SafetyError``
    # subclass — the more specific type must be tested before the base
    # for the ``--overwrite`` branch to fire.
    if isinstance(exc, CollisionError):
        _render_safety_error(
            exc,
            header="Filesystem collision detected:",
            offer_overwrite=True,
        )
        raise SystemExit(exc.exit_code) from None

    if isinstance(exc, SafetyError):
        _render_safety_error(
            exc,
            header=f"Filesystem safety refused the plan during {exc.operation}:",
            offer_overwrite=False,
        )
        raise SystemExit(exc.exit_code) from None

    # -------------------------------------------------------------------------
    # 3. ForgeError and subclasses — the user's fault, with an exit code.
    # -------------------------------------------------------------------------
    # ``MissingRequiredFlagsError`` is handled here directly: its
    # ``__post_init__`` composes an actionable message, and its
    # class-level ``hint`` provides the remediation. No special-case
    # needed — the generic branch renders both fields, which is exactly
    # what the subclass provides.
    if isinstance(exc, ForgeError):
        print_error(str(exc))
        hint = getattr(exc, "hint", None)
        if hint:
            print_hint(hint)
        raise SystemExit(exc.exit_code) from None

    # -------------------------------------------------------------------------
    # 4. Click exceptions — the user's fault, but from Click's parser.
    # -------------------------------------------------------------------------
    if isinstance(exc, click.ClickException):
        print_error(exc.format_message())
        raise SystemExit(exc.exit_code) from None

    # -------------------------------------------------------------------------
    # 5. Unexpected exceptions — bugs, with no internal details leaked.
    # -------------------------------------------------------------------------
    if debug:
        raise exc

    print_error("An unexpected internal error occurred.")
    print_hint(
        "Run with --debug to see the full traceback, or report this at "
        "https://github.com/thapelomagqazana/forge-cli/issues"
    )
    raise SystemExit(ExitCode.INTERNAL_ERROR) from None


# -----------------------------------------------------------------------------
# Internal renderers
# -----------------------------------------------------------------------------
def _render_safety_error(
    exc: SafetyError,
    *,
    header: str,
    offer_overwrite: bool,
) -> None:
    """Render a safety error: header, findings, optional overwrite hint.

    Shared by ``SafetyError`` and ``CollisionError`` so the finding
    rendering is identical (which is what makes the two error
    categories recognisable as the same class of problem). The two
    callers differ only in the header text and whether they name
    ``--overwrite``.

    Why a helper, not duplicated code
    ---------------------------------
    The finding shape is a 4-tuple ``(code, path, message, hint)`` for
    both types, and both render the header in the same style. A future
    field (a ``severity``, a ``suggestion``) would need to appear in
    two places if this were duplicated; a single function is the seam
    for that change.

    Why ``print_error_indented``, not ``print_error``
    -------------------------------------------------
    The CLI's error vocabulary distinguishes top-level failures
    (``print_error``) from detail lines under a header
    (``print_error_indented``). Finding lines are detail lines;
    rendering them at top level would make a five-finding error look
    like five separate failures. The indentation is the visual cue
    that they share a cause.

    Why the hint order is findings-then-hint
    ----------------------------------------
    The user should read *what went wrong* (each finding) before
    *what to do about it* (the top-level hint). This matches the
    loading and planning renderers, which also put the diagnosis
    before the remediation.

    Why the ``hint`` check is ``if hint := ...`` rather than
    ``getattr(exc, "hint", None)`` followed by a truthiness check
    -----------------------------------------------------------------
    ``SafetyError.hint`` is annotated ``str | None`` on
    ``forge.core.exceptions.SafetyError`` — a hint is optional,
    because not every safety failure has a remediation the class
    itself can name. (For ``CollisionError`` the branch below is
    never reached: ``offer_overwrite=True`` skips it. For plain
    ``SafetyError``, ``hint`` may be ``None`` when the caller
    constructed the error without one — the safety-layer callers in
    ``cli.commands.create`` always supply a hint, but the class
    does not require it.)

    ``if hint := getattr(exc, "hint", None)`` both reads the
    attribute and narrows its type to ``str`` inside the body —
    the walrus operator's value is the assignment result, and the
    truthiness check rules out ``None`` and ``""``. A bare
    ``getattr(exc, "hint", None)`` returns ``str | None`` (because
    the default is ``None``), which ``print_hint``'s ``str``
    parameter rejects under mypy strict.
    """
    print_error(header)
    for code, path, message, _finding_hint in exc.findings:
        rendered_path = path or "<root>"
        print_error_indented(f"  [{code}] {rendered_path}: {message}")

    if offer_overwrite:
        print_hint(
            "Re-run with --overwrite to replace the existing files "
            "(a timestamped backup will be created), or edit the plan "
            "so it does not target those paths."
        )
    elif hint := getattr(exc, "hint", None):
        print_hint(hint)
