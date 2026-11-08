"""The CLI's voice — standardized output helpers.

Every user-facing message in Forge CLI goes through this module. The
helpers are deliberately few and simple:

- ``print_success`` — green, checkmark prefix, stdout.
- ``print_warning`` — yellow, warning prefix, stderr.
- ``print_error`` — red, error prefix, stderr.
- ``print_info`` — plain text, stdout.
- ``print_section`` — bold header, stdout. Used by interactive
  wizards to delimit steps, and by any command that groups output.
- ``print_hint`` — dim remediation line, stderr.
- ``print_cancelled`` — dim acknowledgement line, stderr.

The distinction between stdout and stderr is not cosmetic. It is the
Unix contract: ``stdout`` is the *product* of the command (the result
the user asked for), ``stderr`` is the *commentary* (errors, warnings,
progress, diagnostics). A script that pipes ``forge list --json`` to
``jq`` must receive valid JSON on stdout, with no error messages mixed
in. ``print_error`` and ``print_warning`` go to stderr for exactly this
reason.

Warning vs. error
-----------------
A **warning** says: the command produced its product, but something
about the result deserves the user's attention (e.g. "no blueprints
match the given filters" — the command answered the question, the
answer was empty). A warning does not imply a non-zero exit.

An **error** says: the command could not produce its product (e.g. "no
blueprints are installed" when the user asked to list them). An error
typically implies a non-zero exit, because the product the user asked
for does not exist.

Both are rendered to stderr, because both are commentary, but they use
different prefixes (``⚠`` vs. ``✗``) so a user scanning output — or a
script that greps for ``✗`` — can distinguish "the command told me
something" from "the command failed."

Why a single module?
--------------------
If every command calls ``typer.echo`` or ``print`` directly, the CLI's
voice drifts. One command says ``[OK]``, another says ``done.``, a
third says ``✓``. Users cannot predict the format, cannot grep for
errors, cannot trust the output. A single module with a small set of
helpers eliminates the drift. Every message goes through the same
voice.

Design principles
-----------------
1. **Two streams, one rule.** Success and info go to stdout; warnings
   and errors go to stderr. This is the Unix contract. Do not break it.
2. **Colour is a courtesy, not a requirement.** Every message must be
   readable in plain text (with the prefixes as the signal). Colour is
   additive. ``NO_COLOR`` and ``FORCE_COLOR`` are honoured.
3. **One console instance.** Rich's ``Console`` handles TTY detection,
   colour, width, and ``--no-color`` internally. We do not reimplement
   any of it — we just use one instance per stream.
4. **No exceptions leak.** These helpers do not raise. If Rich fails
   (e.g., a broken pipe), the helper catches the failure and falls back
   to ``typer.echo`` so the CLI never crashes because of a formatting
   error.
5. **The caller owns the message; the helper owns the frame.** Helpers
   add a prefix (``✓``, ``⚠``, ``✗``, ``Hint:``) and a colour, and
   nothing else. They do not add leading blank lines, trailing blank
   lines, or section separators the caller did not ask for. A helper
   that adds layout the caller cannot remove is a helper that fights
   the caller — and the caller always wins in the end, either by
   working around the helper (drift) or by removing it (dead code).

Why ``print_section`` is a first-class helper
----------------------------------------------
Interactive wizards group questions by step ("Project setup",
"Blueprint variables"). Without a section header, a long wizard reads
as an undifferentiated wall of prompts and the user loses their place.
The header belongs to the *product* stream (stdout) because it is
part of the wizard's output — it is not an error, a warning, or a
diagnostic. It is not "info" either: info is a single line of data,
while a section header is structural. Putting it on stdout, styled
bold, is the correct home.

The alternative — having the wizard call ``print_info`` with a
``──── Project setup ────`` string — would push layout into the
caller, which is how drift starts: two wizards render headers two
ways, and the third wizard copies the wrong one. The helper exists so
the *look* of a section is decided once, here.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console

# -----------------------------------------------------------------------------
# The console
# -----------------------------------------------------------------------------
# A single console instance for the whole CLI. Rich handles:
#   - TTY detection (colour only if stdout is a terminal).
#   - ``NO_COLOR`` and ``FORCE_COLOR`` environment variables.
#   - Terminal width detection.
#   - Unicode fallback on terminals that cannot render it.
#
# ``stderr=False`` is the default (Console writes to stdout unless told
# otherwise). We create *two* consoles: one for stdout, one for stderr.
# The helpers pick the right one. This is the standard Rich pattern for
# CLIs that must separate product from commentary.
_stdout_console = Console(highlight=False, soft_wrap=False)
_stderr_console = Console(stderr=True, highlight=False, soft_wrap=False)


# -----------------------------------------------------------------------------
# Feature detection
# -----------------------------------------------------------------------------
def _is_colour_enabled() -> bool:
    """Return ``True`` if colour should be emitted.

    Rich already handles ``NO_COLOR`` and ``FORCE_COLOR`` when it creates
    a console, but the helper is exposed for tests and for future
    consumers that need to know whether to emit colour codes themselves.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def print_success(message: str) -> None:
    """Print a success message to stdout, in green, with a checkmark.

    Success is the *product* of a command — the thing the user asked
    for. It goes to stdout so that ``forge list --json`` and similar
    machine-readable commands can be piped without pollution.

    Args:
        message: The message to print. Rendered as ``✓ <message>``.
    """
    try:
        _stdout_console.print(f"[green]✓[/green] {message}")
    except (BrokenPipeError, OSError):
        # The consumer closed the pipe (e.g., `forge list | head`).
        # This is not an error — it is normal for a piped CLI. Fall
        # back to a plain echo and suppress the failure.
        typer_echo_safe(message)


def print_warning(message: str) -> None:
    """Print a warning message to stderr, in yellow, with a prefix.

    Warnings are *commentary* — they tell the user something is
    suboptimal, but the command did what they asked. They go to stderr
    for the same reason as errors: the product stream must not be
    polluted.

    The distinction from ``print_error`` is deliberate and load-bearing:

    * A warning does not imply a non-zero exit. The command succeeded;
      it just has something to say about the result.
    * The prefix (``⚠``) differs from the error prefix (``✗``) so a
      user — or a script grepping for failures — can tell them apart.
    * A script that pipes ``forge list --json | jq`` gets JSON on
      stdout; if the result set is empty (a *valid* outcome), a
      warning goes to stderr and the pipeline still succeeds.

    Mixing warnings and errors into one helper would force callers to
    decide between "prefix this with the alarming symbol" and "use the
    wrong exit code" — both wrong, and both would drift into
    workarounds.

    Args:
        message: The message to print. Rendered as ``⚠ <message>``.
    """
    try:
        _stderr_console.print(f"[yellow]⚠[/yellow] {message}")
    except (BrokenPipeError, OSError):
        typer_echo_safe(message, err=True)


def print_error(message: str) -> None:
    """Print an error message to stderr, in red, with a prefix.

    Errors go to stderr because they are not the product — they are the
    reason the product could not be produced. A script that pipes
    ``forge create --json`` to ``jq`` must receive valid JSON on the
    success path and a clean error on the failure path. Mixing the two
    would break the script.

    Args:
        message: The message to print. Rendered as ``✗ <message>``.
    """
    try:
        _stderr_console.print(f"[red]✗[/red] {message}")
    except (BrokenPipeError, OSError):
        typer_echo_safe(message, err=True)


def print_cancelled(message: str = "Cancelled.") -> None:
    """Print a cancellation message to stderr, dimmed, with no prefix.

    A cancellation (Ctrl+C, ``typer.Abort``, a user declining a
    prompt) is neither "info" nor "warning" nor "error". It is its own
    category:

    - It is not *info*: info is the product of the command, and a
      cancellation produces no product.
    - It is not a *warning*: warnings mean "the command ran, but
      something is suboptimal." A cancellation means "the command
      did not run."
    - It is not an *error*: the user cancelled deliberately. The
      "Error:" prefix would be misleading.

    So it gets its own helper. The helper writes to stderr (the
    commentary stream), uses dim formatting (the message is
    informational, not alarming), and has no prefix (there is
    nothing to prefix — the message is self-evident).

    Args:
        message: The message to print. Defaults to ``"Cancelled."``.
            A custom message is useful for prompt rejections
            ("Aborted by user.") that share the same semantics.
    """
    try:
        _stderr_console.print(f"[dim]{message}[/dim]")
    except (BrokenPipeError, OSError):
        typer_echo_safe(message, err=True)


def print_info(message: str) -> None:
    """Print an informational message to stdout.

    Info is *product* — it is what the user asked for (e.g., the
    result of ``forge --version``). It goes to stdout.

    Args:
        message: The message to print. Rendered as plain text.
    """
    try:
        _stdout_console.print(message)
    except (BrokenPipeError, OSError):
        typer_echo_safe(message)


def print_section(title: str) -> None:
    """Print a section header to stdout, in bold.

    Used by interactive wizards to delimit steps, and by any command
    that groups output into named sections. The header is part of the
    *product* stream (stdout) because it is structural output the
    user asked for, not commentary.

    The rendering is deliberately spare — a bold title, no decoration
    (no box, no rule, no emoji). Decoration competes with the content
    it is meant to frame; a bold title is enough to establish visual
    hierarchy on any terminal, including ones that cannot render
    Unicode. A caller that wants a rule can print one via
    ``print_info`` before calling ``print_section`` — but no caller
    in the current codebase does, and the helper should not presume
    it.

    Args:
        title: The section title. Printed verbatim; the caller is
            responsible for capitalization and punctuation.
    """
    try:
        _stdout_console.print(f"[bold]{title}[/bold]")
    except (BrokenPipeError, OSError):
        typer_echo_safe(title)


def print_hint(message: str) -> None:
    """Print a remediation hint to stderr, in dim text.

    Hints are *commentary* — they accompany an error and tell the user
    what to do next. They go to stderr, dimmed, so they are visible
    next to the error but not confused with the product.

    Args:
        message: The hint to print. Rendered as ``Hint: <message>``
        in dim text.
    """
    try:
        _stderr_console.print(f"[dim]Hint: {message}[/dim]")
    except (BrokenPipeError, OSError):
        typer_echo_safe(f"Hint: {message}", err=True)


# -----------------------------------------------------------------------------
# Short aliases
# -----------------------------------------------------------------------------
# The ``print_*`` names are the canonical API — they are what
# ``handle_exception`` and the tests reference, and they are
# greppable. The short names below are re-exports, not wrappers:
# ``section is print_section``, not ``section() { print_section() }``.
#
# Why bother: ``ui.section("Project setup")`` reads like a method on a
# UI namespace, which it is; ``ui.print_section("Project setup")``
# reads like a function on a printer, which it is not. Command code
# (the wizard in particular, and ``forge list``) reads better with the
# short form, and re-exports are zero-cost — a caller that prefers the
# long form gets the same object.
#
# Both spellings are in ``__all__`` so linters do not flag them as
# unused and so ``from forge.cli.ui import *`` exposes both. If the
# team ever prefers one spelling, deleting the other is a one-line
# change; both are already used in the codebase.
section = print_section
info = print_info
success = print_success
warning = print_warning
warn = print_warning
error = print_error
hint = print_hint
cancelled = print_cancelled


# -----------------------------------------------------------------------------
# Fallback
# -----------------------------------------------------------------------------
def typer_echo_safe(message: str, *, err: bool = False) -> None:
    """Fallback for when Rich cannot render (e.g., broken pipe).

    Rich's rendering can raise ``BrokenPipeError`` when the consumer
    closes the pipe (e.g., ``forge list | head``). This is not an error
    — it is normal for a piped CLI. The fallback writes the raw message
    via ``typer.echo`` (which is ``print`` under the hood) and does not
    raise.

    The function is public so tests can call it directly.
    """
    try:
        import typer

        typer.echo(message, err=err)
    except (BrokenPipeError, OSError):
        # Even the fallback failed. There is nothing more we can do.
        # Do not raise — the CLI should not crash because of a broken
        # pipe.
        pass


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
__all__ = [
    "cancelled",
    "error",
    "hint",
    "info",
    "print_cancelled",
    "print_error",
    "print_hint",
    "print_info",
    "print_section",
    "print_success",
    "print_warning",
    "section",
    "success",
    "typer_echo_safe",
    "warn",
    "warning",
]
