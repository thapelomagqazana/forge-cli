"""The CLI's voice — standardized output helpers.

Every user-facing message in Forge CLI goes through this module. The
helpers are deliberately few and simple:

- ``print_success`` — green, checkmark prefix, stdout.
- ``print_warning`` — yellow, warning prefix, stderr.
- ``print_error`` — red, error prefix, stderr.
- ``print_info`` — plain text, stdout.

The distinction between stdout and stderr is not cosmetic. It is the
Unix contract: ``stdout`` is the *product* of the command (the result
the user asked for), ``stderr`` is the *commentary* (errors, warnings,
progress, diagnostics). A script that pipes ``forge list --json`` to
``jq`` must receive valid JSON on stdout, with no error messages mixed
in. ``print_error`` and ``print_warning`` go to stderr for exactly this
reason.

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
   any of it — we just use one instance.
4. **No exceptions leak.** These helpers do not raise. If Rich fails
   (e.g., a broken pipe), the helper catches the failure and falls back
   to ``typer.echo`` so the CLI never crashes because of a formatting
   error.
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
        _stderr_console.print(f"\n[dim]{message}[/dim]")
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
    "print_cancelled",
    "print_error",
    "print_hint",
    "print_info",
    "print_success",
    "print_warning",
    "typer_echo_safe",
]
