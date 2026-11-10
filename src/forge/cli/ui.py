"""The CLI's voice — standardized output helpers.

Every user-facing message in Forge CLI goes through this module. The
helpers are deliberately few and simple:

- ``print_success`` — green, checkmark prefix, stdout.
- ``print_warning`` — yellow, warning prefix, stderr.
- ``print_error`` — red, error prefix, stderr.
- ``print_error_indented`` — detail line under an error header,
  bullet prefix, stderr. Used by the exception handler to render
  structured findings (safety, planning, conditions) beneath the
  single-line header that names their cause.
- ``print_info`` — plain text, stdout.
- ``print_section`` — bold header, stdout. Used by interactive
  wizards to delimit steps, and by any command that groups output.
- ``print_hint`` — dim remediation line, stderr.
- ``print_cancelled`` — dim acknowledgement line, stderr.
- ``emit`` — raw passthrough for pre-rendered payloads (JSON, tables).

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
3. **One console instance per stream.** Rich's ``Console`` handles TTY
   detection, colour, width, and ``--no-color`` internally. We do not
   reimplement any of it — we use one instance for stdout and one for
   stderr.
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
6. **Payloads flow through ``emit``.** Commands that produce a
   pre-rendered payload (JSON from ``forge list --json``, a formatted
   table from ``forge list``, a template's output) call ``emit``,
   which is a raw passthrough to stdout with the same broken-pipe
   safety as the framed helpers. This is the seam that lets renderers
   stay pure (they return a string) while the CLI still owns the
   stream decision and the broken-pipe fallback.
7. **Colour is applied at emit, never in the payload.** Renderers
   (``cli/renderers/``) must return ANSI-free strings so that JSON
   stays valid and snapshot fixtures stay diffable. If a future helper
   needs to colour a pre-rendered payload, it must do so by framing
   (prefix/suffix), not by injecting escape codes into the payload
   itself — otherwise the renderer's contract is broken by the
   caller, which is exactly the drift principle 5 exists to prevent.

Why ``print_error_indented`` is a first-class helper
----------------------------------------------------
Errors with structured findings (safety refusals, collisions, planning
errors, condition errors) render as a *header* plus zero or more
*detail lines*. The header names the cause; each detail names one
concrete violation. Rendering both with ``print_error`` would give
every detail its own ``✗`` prefix, and the report would read as six
independent failures rather than one failure with six symptoms.

The helper's job is to make that distinction visible:

    ✗ Filesystem collision detected:                 ← print_error
        • [collision.exists] src/a.py: destination … ← print_error_indented
        • [collision.exists] README.md: destination … ← print_error_indented
        hint: Re-run with --overwrite to replace …    ← print_hint

Three things are load-bearing about the indented form:

* **The prefix is a bullet (``•``), not the error symbol.** A bullet
  says "part of a list"; the error symbol says "the error itself".
  Using ``✗`` on every line would make a five-finding report look like
  five failures, which is the opposite of what a grouped report says.
* **The stream is stderr**, matching the header. Findings belong to
  the error report; splitting them onto stdout would break the
  "product vs commentary" contract that the whole module exists to
  enforce.
* **The caller supplies the message, including its own prefix, if it
  wants one.** The helper does *not* prepend ``  • `` — the caller
  does, because the caller knows what shape of detail it is emitting.
  A helper that prepends a prefix would be surprising (callers that
  want ``•`` get ``• •``) and would fight the caller for the layout
  the caller is trying to produce.

Why the caller's message is *not* prefixed by the helper:

    Different callers use different detail prefixes. The exception
    handler uses ``  • [code] path: message``; a future dry-run
    renderer might use ``    [render] dest ← src`` for its detail
    lines. If the helper prepended its own ``•``, the dry-run
    renderer would need a special "don't prefix" variant — exactly
    the drift principle 5 prohibits. Keeping the helper a pure
    stream-selector-plus-nothing lets every caller produce the
    detail format it needs.

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

Why ``emit`` is separate from ``print_info``
--------------------------------------------
``print_info`` routes a message through Rich, which applies wrapping,
markup, and TTY-aware formatting. That is correct for human commentary
— but wrong for machine-readable payloads, where the *bytes* are the
contract. A JSON payload must not be re-wrapped, must not have markup
interpreted, and must not have its whitespace normalised. ``emit``
bypasses Rich entirely and writes the payload as-is to stdout, with
only the broken-pipe fallback retained. Two helpers, two contracts:
framed for humans, raw for machines.

Why the aliases are re-exports, not wrappers
--------------------------------------------
``section is print_section`` (identity, not delegation). Re-exports are
zero-cost, and they mean a caller that patches one name patches both —
a subtlety that matters for tests. A wrapper would break that identity
and force tests to know which spelling a given call site used, which is
exactly the kind of accidental coupling the aliases exist to avoid.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console

# -----------------------------------------------------------------------------
# The consoles
# -----------------------------------------------------------------------------
# Two console instances — one per stream. Rich handles:
#   - TTY detection (colour only if the stream is a terminal).
#   - ``NO_COLOR`` and ``FORCE_COLOR`` environment variables.
#   - Terminal width detection.
#   - Unicode fallback on terminals that cannot render it.
#
# ``stderr=True`` on the second instance is what makes the split work:
# Rich writes to the right stream based on construction, so the helper
# bodies never pass ``file=`` explicitly. That is the single point of
# decision this module's "two streams, one rule" principle reduces to.
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
# Framed helpers (human commentary)
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


def print_error_indented(message: str) -> None:
    """Print a detail line under an error header, on stderr.

    Structured error reports — safety refusals, collisions, planning
    errors, condition errors — have a header (rendered by
    ``print_error``) plus zero or more detail lines, one per finding.
    This helper renders those details.

    The message is emitted **verbatim** (aside from a small dim
    colour), so the caller owns the full detail format — including
    any bullet prefix (``  • ``) or code tag (``[collision.exists]``)
    they want to include. The helper's only job is:

    * **stream selection** — stderr, matching the header. Splitting
      findings onto stdout would break the product-vs-commentary
      contract that the rest of this module enforces.
    * **broken-pipe safety** — same fallback as the other helpers.

    Rendering the message with ``[dim]`` (rather than ``[red]``) is
    deliberate: the *header* carries the error colour and the error
    symbol, so the user's eye lands there first. Detail lines are
    subordinate; dimming them establishes the visual hierarchy that
    makes a five-finding report read as one failure with five
    symptoms, not five separate failures.

    Why no bullet prefix from the helper itself:
        Different callers use different detail formats. The exception
        handler uses ``  • [code] path: message``; a future dry-run
        renderer might use ``    [render] dest ← src``. If the helper
        prepended its own ``•``, callers who want a bullet would get
        ``• •`` and callers who want a different prefix would need a
        "don't prefix" variant — exactly the caller-vs-helper friction
        the module's "caller owns the message" principle exists to
        prevent.

    Args:
        message: The detail line to print, including any prefix the
            caller wants. Rendered in dim text, verbatim otherwise.
    """
    try:
        _stderr_console.print(f"[dim]{message}[/dim]")
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
# Raw passthrough (machine payloads)
# -----------------------------------------------------------------------------
def emit(payload: str) -> None:
    """Write a pre-rendered payload to stdout, unchanged.

    This is the machine-payload seam. Commands that produce output a
    renderer has already formatted — JSON from ``forge list --json``,
    a table from ``forge list``, a rendered template — call ``emit``
    rather than ``print_info``, because ``print_info`` routes through
    Rich, which wraps, interprets markup, and normalises whitespace.
    None of those are safe for a payload whose *bytes* are the
    contract.

    ``emit`` writes the payload verbatim to stdout with a single
    trailing newline (POSIX text convention: a line ends in a
    newline). It does not add a leading newline, does not add
    colour, does not interpret markup, and does not reformat.

    It retains the same broken-pipe safety as the framed helpers: if
    the consumer closes the pipe (``forge list | head``), the write
    failure is caught and the payload is silently dropped — writing
    to a closed pipe is normal for a piped CLI, not an error.

    Contract notes for renderers
    ----------------------------
    * Renderers must return ANSI-free strings. Colour is a framing
      concern owned by ``ui``, and injecting escape codes into a
      payload breaks JSON validity and snapshot determinism.
    * Renderers must return strings without a trailing newline.
      ``emit`` owns line termination so that every payload ends with
      exactly one newline, regardless of which renderer produced it.
      A renderer that appends its own newline would produce two, and
      the two-renders-one-newline invariant is what makes golden
      fixtures diffable.

    Args:
        payload: The pre-rendered payload to write to stdout. Emitted
            verbatim, followed by one newline.
    """
    try:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        typer_echo_safe(payload)


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
error_indented = print_error_indented
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
    "emit",
    "error",
    "error_indented",
    "hint",
    "info",
    "print_cancelled",
    "print_error",
    "print_error_indented",
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
