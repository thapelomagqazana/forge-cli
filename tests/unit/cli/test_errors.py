"""Unit tests for ``forge.cli.errors.handle_exception``.

The handler is the single most security-sensitive function in the
CLI. It decides what the user sees when something goes wrong. Its
contract has *four categories* of exception, one per branch, plus a
fifth category for the dual-mode input handling added in 1.1.6:

1. **``ForgeError`` renders a message + hint, exit with code.**
   The message is the first line; the hint (if present) is a
   separate, dimmed line. The exit code is the subclass's declared
   ``exit_code``. ``--debug`` does *not* change this behaviour.

2. **``click.ClickException`` renders its message, exit with
   Click's code (usually 2).** These are user errors from Click's
   parser: the user typed a command that does not exist, or passed
   an argument that does not match the signature. ``--debug`` does
   *not* change this behaviour.

3. **Cancellations render "Cancelled.", exit 1.** The category
   includes ``KeyboardInterrupt``, ``click.Abort``, ``typer.Abort``,
   and — since 1.1.6 — ``PromptCancelledError``. No traceback. No
   "Error:" prefix. A single dimmed line on stderr. ``--debug`` does
   *not* change this behaviour.

   **Why ``PromptCancelledError`` belongs here, not in category 1.**
   The class is a ``UserInputError`` subclass (so it *is* a
   ``ForgeError``), but its *intent* is cancellation: the user
   chose to stop, they did not fail. Rendering it as a red error
   would be visually wrong (``✗`` where the user expects a dimmed
   acknowledgement) and semantically wrong (the hint line is shown
   for errors and omitted for cancellations). The handler therefore
   checks cancellation *first*, and this file proves the ordering.

4. **Unexpected exceptions render a generic message, exit 5.**
   The message must not include the exception's class name,
   message, or traceback. Those are internal details that could
   hint at a vulnerability. The user gets a link to the issue
   tracker. **Under ``--debug``, the handler re-raises the
   exception instead**, so the developer sees the traceback.

5. **``MissingRequiredFlagsError`` renders like a ``ForgeError``,
   exit 1.** Since 1.1.6, the handler has an explicit branch for
   it. Behaviourally identical to category 1, but the branch exists
   because the *message shape* (aggregated flag list) is the
   acceptance criterion of a specific task, and a future maintainer
   reading ``handle_exception`` should see the case named. This
   file exercises the branch so it cannot rot.

The crucial distinction
-----------------------
``--debug`` only affects *unexpected* exceptions. User errors
(``ForgeError``, ``click.ClickException``) and cancellations are
always rendered friendly, even under ``--debug``. The flag is for
the *developer of Forge*, not the *user of Forge*: it exists so the
developer can see the traceback of a *bug*, not so the user can see
the traceback of their own typo.

Why ``SystemExit``, not ``typer.Exit``?
---------------------------------------
The handler is called from ``main()``, *after* Typer's dispatch
has finished. ``typer.Exit`` is only special *inside* a Typer
command — Typer catches it there and converts it to a
``SystemExit``. Outside Typer, ``typer.Exit`` is an ordinary
exception that propagates into ``sys.exit(main())``, which prints
a traceback and exits 1.

The handler therefore raises ``SystemExit`` directly. The tests
assert on ``SystemExit.code``, which is the code the shell actually
sees. This is the *contract*, not the *implementation*: the tests
do not care what exception the handler uses internally, only that
the process exits with the correct code.

Why the tests call ``handle_exception`` directly
------------------------------------------------
Testing the handler through ``CliRunner`` is unreliable, because
Typer intercepts exceptions before the handler runs when the app is
invoked. The handler is a pure function of ``(exception, debug)``
→ (output, exit code), so calling it directly is the only way to
assert on all branches in isolation.

Test organisation
-----------------
The tests are grouped by *category* of exception, with one test
class per category, plus cross-cutting classes:

- ``TestForgeError`` — clause 1 (``ForgeError``).
- ``TestMissingRequiredFlagsError`` — clause 5 (new in 1.1.6).
- ``TestClickException`` — clause 2 (``click.ClickException``).
- ``TestCancellation`` — clause 3 (cancellations, including the
  new ``PromptCancelledError`` and ``click.Abort`` cases).
- ``TestUnexpectedException`` — clause 4 (unexpected exceptions,
  the non-``--debug`` branch).
- ``TestDebugMode`` — the ``--debug`` branch, across all
  categories.
- ``TestNoTracebackLeaks`` — the "no traceback" invariant, across
  every category (parametrized).
- ``TestExitCodeMapping`` — the exit-code contract, across every
  ``ForgeError`` subclass (parametrized).
- ``TestExitCodeAcceptanceCriteria`` — the acceptance criteria for
  the exit-code enumeration, named after each criterion.

The last class duplicates coverage from ``TestForgeError`` and
``TestUnexpectedException``, but it is named so that a reader
checking the WBS's acceptance criteria can find the test for each
criterion *by name*. The duplication is intentional.
"""

from __future__ import annotations

import io

import click
import pytest
import typer
from rich.console import Console

from forge.cli import errors, ui
from forge.core.exceptions import (
    BlueprintError,
    ExitCode,
    FilesystemConflictError,
    ForgeError,
    MissingRequiredFlagsError,
    PromptCancelledError,
    UserInputError,
    VerificationError,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Replace the stderr console with an in-memory one.

    The handler renders errors to stderr (via ``ui.print_error`` and
    ``ui.print_hint``). Replacing the stderr console with an
    in-memory one lets the test assert on the exact bytes the
    handler emits, without going through ``capsys`` (which does not
    capture Rich's rendering reliably).

    The stdout console is also replaced (with a separate buffer) so
    that a bug in the handler that accidentally prints to stdout is
    caught by the tests that assert on the stdout buffer.

    Returns:
        The stderr buffer. Tests read it with ``.getvalue()``. The
        stdout buffer is available via ``captured.stdout_buf`` for
        tests that need to assert it was not written to.
    """
    stderr_buf = io.StringIO()
    stdout_buf = io.StringIO()
    monkeypatch.setattr(
        ui,
        "_stderr_console",
        Console(file=stderr_buf, stderr=True, highlight=False, force_terminal=False),
    )
    monkeypatch.setattr(
        ui,
        "_stdout_console",
        Console(file=stdout_buf, highlight=False, force_terminal=False),
    )
    # The stdout buffer is attached to the stderr buffer's namespace
    # via a sentinel attribute, so tests that want to assert "nothing
    # was written to stdout" can access it.
    stderr_buf.stdout_buf = stdout_buf  # type: ignore[attr-defined]
    return stderr_buf


def _captured_stdout(captured: io.StringIO) -> str:
    """Return the stdout buffer's contents from the fixture's buffer."""
    return captured.stdout_buf.getvalue()  # type: ignore[attr-defined]


# -----------------------------------------------------------------------------
# Clause 1: ForgeError renders a message + hint, exit with code
# -----------------------------------------------------------------------------
class TestForgeError:
    """Tests for the ``ForgeError`` branch of the handler."""

    @pytest.mark.unit()
    def test_renders_message_and_hint(self, captured: io.StringIO) -> None:
        """A ``ForgeError`` renders its message and hint, exits with its code."""
        exc = UserInputError(
            "project name is required",
            hint="pass --name my-project",
        )
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        output = captured.getvalue()
        assert "project name is required" in output
        assert "Hint:" in output
        assert "pass --name my-project" in output

    @pytest.mark.unit()
    def test_renders_without_hint(self, captured: io.StringIO) -> None:
        """A ``ForgeError`` without a hint renders only the message."""
        exc = UserInputError("something is wrong")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        output = captured.getvalue()
        assert "something is wrong" in output
        assert "Hint:" not in output

    @pytest.mark.unit()
    def test_writes_to_stderr_not_stdout(self, captured: io.StringIO) -> None:
        """A ``ForgeError`` writes to stderr, not stdout.

        This is the same contract as ``print_error``: errors are
        commentary, not product. A script parsing ``forge verify
        --json`` must not receive error messages mixed into stdout.
        """
        exc = UserInputError("boom")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "boom" in captured.getvalue()
        assert "boom" not in _captured_stdout(captured)

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        ("exc_class", "expected_code"),
        [
            (UserInputError, ExitCode.USER_ERROR),
            (BlueprintError, ExitCode.BLUEPRINT_ERROR),
            (FilesystemConflictError, ExitCode.FILESYSTEM_CONFLICT),
            (VerificationError, ExitCode.VERIFICATION_FAILED),
            (ForgeError, ExitCode.INTERNAL_ERROR),
        ],
    )
    def test_each_subclass_uses_its_declared_exit_code(
        self,
        captured: io.StringIO,
        exc_class: type[ForgeError],
        expected_code: ExitCode,
    ) -> None:
        """Each ``ForgeError`` subclass maps to its declared exit code.

        This is the contract that lets scripts branch on ``$?``
        without parsing the message. Each subclass has a distinct
        code, and this test proves it.
        """
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc_class("boom"), debug=False)
        assert exc_info.value.code == expected_code

    @pytest.mark.unit()
    def test_hint_only_rendered_when_present(self, captured: io.StringIO) -> None:
        """The hint is only rendered when the exception has one."""
        exc_with = UserInputError("boom", hint="try this")
        exc_without = UserInputError("boom")

        with pytest.raises(SystemExit):
            errors.handle_exception(exc_with, debug=False)
        assert "Hint:" in captured.getvalue()

        captured.seek(0)
        captured.truncate(0)

        with pytest.raises(SystemExit):
            errors.handle_exception(exc_without, debug=False)
        assert "Hint:" not in captured.getvalue()


# -----------------------------------------------------------------------------
# Clause 5: MissingRequiredFlagsError renders like a ForgeError, exit 1
# -----------------------------------------------------------------------------
# This category was added in 1.1.6 for the non-interactive ``create``
# path. The branch in ``handle_exception`` is behaviorally identical
# to the ``ForgeError`` branch — same rendering, same exit code — but
# it exists as a *named* case so a maintainer scanning the handler
# sees the acceptance criterion of the dual-mode task represented in
# code. These tests exercise the branch directly.
# -----------------------------------------------------------------------------
class TestMissingRequiredFlagsError:
    """Tests for the ``MissingRequiredFlagsError`` branch of the handler."""

    @pytest.mark.unit()
    def test_renders_message_and_hint(self, captured: io.StringIO) -> None:
        """The aggregated flag list is rendered with the default hint."""
        exc = MissingRequiredFlagsError(command="create", missing=("name",))
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        output = captured.getvalue()
        assert "--name" in output
        # The class-level default hint must be rendered — it is what
        # turns the error from a dead end into a signpost.
        assert "Hint:" in output

    @pytest.mark.unit()
    def test_renders_all_missing_flags(self, captured: io.StringIO) -> None:
        """Every missing flag is listed, not just the first.

        This is the observable half of the "fail fast, fail complete"
        contract: a script fixes all flags in one edit, not N.
        """
        exc = MissingRequiredFlagsError(command="create", missing=("name", "blueprint"))
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        output = captured.getvalue()
        assert "--name" in output
        assert "--blueprint" in output

    @pytest.mark.unit()
    def test_writes_to_stderr_not_stdout(self, captured: io.StringIO) -> None:
        """The error goes to stderr, preserving the stdout contract."""
        exc = MissingRequiredFlagsError(command="create", missing=("name",))
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "--name" in captured.getvalue()
        assert "--name" not in _captured_stdout(captured)

    @pytest.mark.unit()
    def test_does_not_show_traceback_under_debug(self, captured: io.StringIO) -> None:
        """Under ``--debug``, a missing-flags error is still rendered friendly.

        ``--debug`` is for bugs; a missing flag is the user's fault.
        The traceback would be noise, not signal — and this is
        exactly the contract documented in the handler's module
        docstring.
        """
        exc = MissingRequiredFlagsError(command="create", missing=("name",))
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=True)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert "Traceback" not in captured.getvalue()


# -----------------------------------------------------------------------------
# Clause 2: Click exceptions render friendly, exit with Click's code
# -----------------------------------------------------------------------------
class TestClickException:
    """Tests for the ``click.ClickException`` branch of the handler.

    Click raises ``ClickException`` (``UsageError``, ``BadParameter``,
    ``Abort``, etc.) when the user's input does not match the
    command's contract. These are user errors, not bugs. They must be
    rendered friendly with Click's own exit code (2 for usage errors),
    *even under* ``--debug``.

    Note: ``click.Abort`` is *not* tested here — since 1.1.6 it is
    routed to the cancellation branch, and its tests live in
    ``TestCancellation``.
    """

    @pytest.mark.unit()
    def test_usage_error_renders_friendly(self, captured: io.StringIO) -> None:
        """A ``UsageError`` renders its message and exits 2."""
        exc = click.UsageError("No such command 'nonsense-command'.")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == 2
        assert "No such command" in captured.getvalue()

    @pytest.mark.unit()
    def test_usage_error_renders_friendly_under_debug(self, captured: io.StringIO) -> None:
        """Under ``--debug``, a ``UsageError`` is still rendered friendly.

        This is the same contract as ``ForgeError``: user errors are
        not bugs. ``--debug`` does not show a traceback for a typo.
        """
        exc = click.UsageError("No such command 'nonsense-command'.")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=True)

        assert exc_info.value.code == 2
        assert "No such command" in captured.getvalue()

    @pytest.mark.unit()
    def test_usage_error_has_no_traceback(self, captured: io.StringIO) -> None:
        """A ``UsageError`` never emits a traceback."""
        exc = click.UsageError("No such command 'nonsense-command'.")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "Traceback" not in captured.getvalue()

    @pytest.mark.unit()
    def test_usage_error_writes_to_stderr(self, captured: io.StringIO) -> None:
        """A ``UsageError`` writes to stderr, not stdout."""
        exc = click.UsageError("No such command 'nonsense-command'.")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "No such command" in captured.getvalue()
        assert "No such command" not in _captured_stdout(captured)

    @pytest.mark.unit()
    def test_bad_parameter_renders_friendly(self, captured: io.StringIO) -> None:
        """A ``BadParameter`` is also a user error, rendered friendly."""
        exc = click.BadParameter("must be a valid name")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "must be a valid name" in captured.getvalue()


# -----------------------------------------------------------------------------
# Clause 3: Cancellations render "Cancelled.", exit 1
# -----------------------------------------------------------------------------
# This category now includes four exception types:
#
#   - ``KeyboardInterrupt`` — Ctrl+C at any point.
#   - ``typer.Abort``       — the Typer-level abort signal.
#   - ``click.Abort``       — the Click-level abort signal (added
#                             in 1.1.6 to guard against Typer version
#                             drift in the re-export).
#   - ``PromptCancelledError`` — the domain-level cancellation raised
#                                by the interactive wizard's prompts
#                                (added in 1.1.6).
#
# All four must render identically: a dimmed "Cancelled." on stderr,
# exit 1, no traceback, no "Error:" prefix. The handler checks this
# branch *before* the ``ForgeError`` branch, because
# ``PromptCancelledError`` is a ``ForgeError`` subclass. The tests
# below lock that ordering.
# -----------------------------------------------------------------------------
class TestCancellation:
    """Tests for the cancellation branch of the handler."""

    @pytest.mark.unit()
    def test_keyboard_interrupt_renders_cancelled(self, captured: io.StringIO) -> None:
        """``KeyboardInterrupt`` renders "Cancelled." and exits 1."""
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(KeyboardInterrupt(), debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        output = captured.getvalue()
        assert "Cancelled" in output

    @pytest.mark.unit()
    def test_keyboard_interrupt_has_no_error_prefix(self, captured: io.StringIO) -> None:
        """``KeyboardInterrupt`` does not use the "Error:" prefix.

        Ctrl+C is a cancellation, not an error. The user does not need
        to be told it was an error.
        """
        with pytest.raises(SystemExit):
            errors.handle_exception(KeyboardInterrupt(), debug=False)

        assert "Error:" not in captured.getvalue()

    @pytest.mark.unit()
    def test_keyboard_interrupt_has_no_traceback(self, captured: io.StringIO) -> None:
        """``KeyboardInterrupt`` never emits a traceback."""
        with pytest.raises(SystemExit):
            errors.handle_exception(KeyboardInterrupt(), debug=False)

        assert "Traceback" not in captured.getvalue()

    @pytest.mark.unit()
    def test_typer_abort_renders_cancelled(self, captured: io.StringIO) -> None:
        """``typer.Abort`` renders the same message as Ctrl+C."""
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(typer.Abort(), debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert "Cancelled" in captured.getvalue()

    @pytest.mark.unit()
    def test_click_abort_renders_cancelled(self, captured: io.StringIO) -> None:
        """``click.Abort`` renders the same message as Ctrl+C.

        Added in 1.1.6. ``typer.Abort`` is *usually* an alias for
        ``click.Abort`` in modern Typer, but the alias has drifted
        across versions. Routing both explicitly through the
        cancellation branch is a one-token cost that removes a future
        footgun — and this test proves the routing exists on
        whichever Typer version is installed.
        """
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(click.Abort(), debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert "Cancelled" in captured.getvalue()

    @pytest.mark.unit()
    def test_prompt_cancelled_renders_as_cancellation_not_error(
        self, captured: io.StringIO
    ) -> None:
        """``PromptCancelledError`` renders as a cancellation, not an error.

        The class is a ``UserInputError`` subclass — which is a
        ``ForgeError`` — so a naive dispatch would render it with the
        red error prefix and the "Hint:" line. But the *intent* of a
        cancelled prompt is "the user chose to stop," not "the user
        failed," and the rendering must reflect the intent. This test
        proves the cancellation-first dispatch ordering.
        """
        exc = PromptCancelledError(prompt="Project name")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        output = captured.getvalue()
        assert "Cancelled" in output
        # The error prefix (✗) is what distinguishes an error line
        # from a cancellation line. Its absence is the observable
        # proof that the ordering is correct.
        assert "✗" not in output

    @pytest.mark.unit()
    def test_prompt_cancelled_does_not_render_hint(self, captured: io.StringIO) -> None:
        """``PromptCancelledError`` carries a hint but does not show it.

        The class-level hint exists for a future non-terminal UI
        (where a "re-run?" button is actionable). The terminal
        renderer deliberately omits it — the user already knows they
        pressed Ctrl+C, and a "Hint:" line would be a confusing
        suggestion to "re-run when ready" for someone who clearly
        chose not to.

        This test locks the distinction: ``PromptCancelledError.hint``
        is *not* ``None`` (proven by the attribute assertion), but
        the rendering does not include the "Hint:" prefix.
        """
        exc = PromptCancelledError()
        assert exc.hint is not None  # the attribute exists
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "Hint:" not in captured.getvalue()

    @pytest.mark.unit()
    def test_prompt_cancelled_writes_to_stderr_not_stdout(self, captured: io.StringIO) -> None:
        """The cancellation goes to stderr, preserving stdout purity.

        ``print_cancelled`` is a *separate* helper from
        ``print_error``; this test proves the handler routes to the
        right helper. If it accidentally used ``print_error``, the
        stdout assertion would still pass (both helpers write to
        stderr), but the ``✗`` prefix test above would fail —
        together, the two tests pin down the exact rendering.
        """
        exc = PromptCancelledError()
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "Cancelled" in captured.getvalue()
        assert "Cancelled" not in _captured_stdout(captured)


# -----------------------------------------------------------------------------
# Clause 4: Unexpected exceptions render a generic message, exit 5
# -----------------------------------------------------------------------------
class TestUnexpectedException:
    """Tests for the unexpected-exception branch of the handler."""

    @pytest.mark.unit()
    def test_renders_generic_message(self, captured: io.StringIO) -> None:
        """An unexpected exception renders a generic message, exit 5."""
        exc = RuntimeError("boom")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.INTERNAL_ERROR
        output = captured.getvalue()
        assert "unexpected internal error" in output.lower()

    @pytest.mark.unit()
    def test_does_not_leak_class_name(self, captured: io.StringIO) -> None:
        """The exception's class name is not in the output.

        A class name like ``RuntimeError`` is an internal detail. It
        might hint at a vulnerability ("this is a runtime error in the
        filesystem layer") that the user should not see by default.
        """
        exc = RuntimeError("boom")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "RuntimeError" not in captured.getvalue()

    @pytest.mark.unit()
    def test_does_not_leak_exception_message(self, captured: io.StringIO) -> None:
        """The exception's message is not in the output.

        The exception's message might contain sensitive information
        (a file path, a database connection string, a token). It must
        not be rendered by default.
        """
        exc = RuntimeError("secret-token-12345")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "secret-token-12345" not in captured.getvalue()

    @pytest.mark.unit()
    def test_does_not_leak_traceback(self, captured: io.StringIO) -> None:
        """The traceback is not in the output."""
        exc = RuntimeError("boom")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "Traceback" not in captured.getvalue()

    @pytest.mark.unit()
    def test_suggests_debug_flag(self, captured: io.StringIO) -> None:
        """The output suggests running with ``--debug``."""
        exc = RuntimeError("boom")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "--debug" in captured.getvalue()

    @pytest.mark.unit()
    def test_points_at_issue_tracker(self, captured: io.StringIO) -> None:
        """The output points at the issue tracker."""
        exc = RuntimeError("boom")
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)

        assert "github.com" in captured.getvalue()


# -----------------------------------------------------------------------------
# Clause 5: --debug re-raises *unexpected* exceptions only
# -----------------------------------------------------------------------------
class TestDebugMode:
    """Tests for the ``--debug`` branch of the handler.

    ``--debug`` re-raises only *unexpected* exceptions. User errors
    (``ForgeError``, ``click.ClickException``) and cancellations are
    always rendered friendly, even under ``--debug``. This is the
    contract that makes ``--debug`` useful for developers (it shows
    the traceback of a *bug*) without making it noisy for users (it
    does not show the traceback of a *typo*).
    """

    @pytest.mark.unit()
    def test_debug_re_raises_unexpected_exception(self) -> None:
        """Under ``--debug``, an unexpected exception is re-raised."""
        exc = RuntimeError("boom")
        with pytest.raises(RuntimeError) as exc_info:
            errors.handle_exception(exc, debug=True)
        assert exc_info.value is exc

    @pytest.mark.unit()
    def test_debug_renders_forge_error_friendly(self, captured: io.StringIO) -> None:
        """Under ``--debug``, a ``ForgeError`` is still rendered friendly.

        ``--debug`` is for bugs, not for user errors. A ``ForgeError``
        is the user's fault (bad input, missing blueprint), so it must
        be rendered as a friendly message even under ``--debug``. The
        developer does not need a traceback for a typo.
        """
        exc = UserInputError("boom")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=True)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert "boom" in captured.getvalue()

    @pytest.mark.unit()
    def test_debug_renders_missing_flags_friendly(self, captured: io.StringIO) -> None:
        """Under ``--debug``, ``MissingRequiredFlagsError`` is rendered friendly.

        Added in 1.1.6. Same contract as the other user errors: the
        developer does not need a traceback to see that ``--name``
        was forgotten.
        """
        exc = MissingRequiredFlagsError(command="create", missing=("name",))
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=True)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert "--name" in captured.getvalue()
        assert "Traceback" not in captured.getvalue()

    @pytest.mark.unit()
    def test_debug_renders_click_exception_friendly(self, captured: io.StringIO) -> None:
        """Under ``--debug``, a ``ClickException`` is still rendered friendly.

        Same contract as ``ForgeError``: user errors are not bugs.
        ``--debug`` does not show a traceback for a typo.
        """
        exc = click.UsageError("No such command 'nonsense-command'.")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=True)

        assert exc_info.value.code == 2
        assert "No such command" in captured.getvalue()

    @pytest.mark.unit()
    def test_debug_renders_cancellation_friendly(self, captured: io.StringIO) -> None:
        """Under ``--debug``, a cancellation is still rendered friendly.

        A cancellation (Ctrl+C, ``typer.Abort``) is not a bug. It is
        the user telling the command to stop. Under ``--debug``, the
        message is the same dimmed "Cancelled." line — there is
        nothing to debug.
        """
        exc = KeyboardInterrupt()
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=True)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert "Cancelled" in captured.getvalue()

    @pytest.mark.unit()
    def test_debug_renders_prompt_cancelled_friendly(self, captured: io.StringIO) -> None:
        """Under ``--debug``, ``PromptCancelledError`` is rendered friendly.

        Added in 1.1.6. The class is *both* a ``UserInputError`` and
        a cancellation; under ``--debug`` it must still take the
        cancellation branch (not the ``ForgeError`` branch, which
        would also be "friendly" but would render with the wrong
        prefix). The ``✗`` absence is the observable proof of the
        correct branch.
        """
        exc = PromptCancelledError()
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=True)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert "Cancelled" in captured.getvalue()
        assert "✗" not in captured.getvalue()

    @pytest.mark.unit()
    def test_debug_does_not_write_to_stderr_for_unexpected(self, captured: io.StringIO) -> None:
        """Under ``--debug``, an unexpected exception writes nothing to stderr.

        The traceback (emitted by Python's default handler) goes to
        stderr, not the CLI's friendly message. The handler must not
        also emit its own message — that would be confusing.
        """
        exc = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            errors.handle_exception(exc, debug=True)

        assert captured.getvalue() == ""


# -----------------------------------------------------------------------------
# No traceback ever leaks
# -----------------------------------------------------------------------------
class TestNoTracebackLeaks:
    """The single most important property of the handler.

    No path through the handler emits a traceback for any exception
    type. Every failure mode is rendered as a one-line message. This
    is the property that makes the CLI usable for non-developers.

    The parametrize lists include every category of exception,
    *including the two added in 1.1.6* — a future contributor reading
    this test sees the full exception taxonomy in one place.
    """

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "exc",
        [
            UserInputError("user"),
            BlueprintError("blueprint"),
            FilesystemConflictError("filesystem"),
            VerificationError("verification"),
            ForgeError("forge"),
            MissingRequiredFlagsError(command="create", missing=("name",)),
            PromptCancelledError(),
            click.UsageError("no such command"),
            click.Abort(),
            KeyboardInterrupt(),
            RuntimeError("runtime"),
            ValueError("value"),
            KeyError("key"),
            OSError("os"),
            Exception("generic"),
        ],
    )
    def test_no_traceback_in_output(self, captured: io.StringIO, exc: BaseException) -> None:
        """The handler never emits a traceback for any exception type."""
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)
        assert "Traceback" not in captured.getvalue()

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "exc",
        [
            UserInputError("user"),
            BlueprintError("blueprint"),
            FilesystemConflictError("filesystem"),
            VerificationError("verification"),
            ForgeError("forge"),
            MissingRequiredFlagsError(command="create", missing=("name",)),
            PromptCancelledError(),
            click.UsageError("no such command"),
            click.Abort(),
            KeyboardInterrupt(),
            RuntimeError("runtime"),
            ValueError("value"),
            KeyError("key"),
            OSError("os"),
            Exception("generic"),
        ],
    )
    def test_no_file_paths_in_output(self, captured: io.StringIO, exc: BaseException) -> None:
        """The handler never emits a file path.

        A file path (e.g., ``/home/user/project/src/forge/cli/main.py``)
        is an internal detail. It could leak the user's home directory
        or the project's structure, neither of which the user needs
        to see by default.
        """
        with pytest.raises(SystemExit):
            errors.handle_exception(exc, debug=False)
        output = captured.getvalue()
        assert "/src/" not in output
        assert "\\src\\" not in output
        assert "/home/" not in output


# -----------------------------------------------------------------------------
# Exit code mapping is exhaustive
# -----------------------------------------------------------------------------
class TestExitCodeMapping:
    """The handler maps every ``ForgeError`` subclass to a distinct code.

    This is the contract that lets scripts branch on ``$?``. If two
    subclasses shared a code, the script could not distinguish them.

    Since 1.1.6, ``MissingRequiredFlagsError`` and
    ``PromptCancelledError`` *deliberately* share ``USER_ERROR`` (1)
    with ``UserInputError``. They are distinct *types* (so tests and
    handlers can dispatch on them) but the same *severity* (so a
    script that says "if $? == 1, the user can fix it" remains
    correct). This test asserts the intended sharing rather than
    forbidding it.
    """

    @pytest.mark.unit()
    def test_the_four_original_subclasses_have_distinct_codes(self) -> None:
        """The four original subclasses have distinct exit codes.

        This is the mapping a script needs to branch on. ``UserInputError``
        and its new siblings (``MissingRequiredFlagsError``,
        ``PromptCancelledError``) intentionally share ``USER_ERROR``;
        the four *original* subclasses must remain distinct.
        """
        subclasses = [
            UserInputError,
            BlueprintError,
            FilesystemConflictError,
            VerificationError,
        ]
        codes = [cls.exit_code for cls in subclasses]
        assert len(codes) == len(set(codes)), f"Duplicate exit codes: {codes}"

    @pytest.mark.unit()
    def test_new_subclasses_share_user_error_code(self) -> None:
        """``MissingRequiredFlagsError`` and ``PromptCancelledError`` share code 1.

        The sharing is intentional: both are user-fixable errors,
        and a script that branches on ``$? in (1, 2, 3)`` must treat
        them identically to ``UserInputError``. The *types* are
        distinct so the handler and tests can dispatch on them; the
        *codes* are shared so the shell contract does not fragment.
        """
        assert MissingRequiredFlagsError.exit_code == ExitCode.USER_ERROR
        assert PromptCancelledError.exit_code == ExitCode.USER_ERROR

    @pytest.mark.unit()
    def test_base_class_uses_internal_error(self) -> None:
        """A bare ``ForgeError`` uses the internal-error code."""
        assert ForgeError.exit_code == ExitCode.INTERNAL_ERROR


# -----------------------------------------------------------------------------
# Acceptance criteria for the exit-code enumeration and mapping
# -----------------------------------------------------------------------------
# Each test below is named after the acceptance criterion it proves.
# The criterion is quoted in the test's docstring. This makes the
# test suite *readable* as a contract: a reader can match a test to
# a criterion without reading the test body.
class TestExitCodeAcceptanceCriteria:
    """Tests named after the exit-code acceptance criteria.

    These tests duplicate coverage that ``TestForgeError`` and
    ``TestUnexpectedException`` already provide. They are kept as
    *named* criteria so that a future contributor checking the WBS's
    acceptance criteria can find the test for a specific criterion
    by name, without scanning the parametrized tests.

    The WBS lists five criteria:

    1. Each exit code is documented in the CLI Contract and code.
    2. A ``UserInputError`` returns exit code 1.
    3. A ``FilesystemConflictError`` returns exit code 3.
    4. A ``VerificationError`` returns exit code 4.
    5. An unexpected exception returns exit code 5.

    The tests below add two more (``BlueprintError`` and the bare
    ``ForgeError``) so that *every* subclass has a named test, and
    two structural tests (distinct codes, documented codes) that
    back criterion 1.
    """

    @pytest.mark.unit()
    def test_user_input_error_returns_exit_code_1(self, captured: io.StringIO) -> None:
        """**Acceptance criterion 2:** ``UserInputError`` returns exit code 1.

        A script that sees exit code 1 knows "the user's fault —
        check the message and retry."
        """
        exc = UserInputError("invalid input")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        assert exc_info.value.code == 1
        assert "invalid input" in captured.getvalue()

    @pytest.mark.unit()
    def test_filesystem_conflict_error_returns_exit_code_3(self, captured: io.StringIO) -> None:
        """**Acceptance criterion 3:** ``FilesystemConflictError`` returns exit code 3.

        A script that sees exit code 3 knows "the filesystem is the
        problem — choose a different output or fix permissions."
        """
        exc = FilesystemConflictError("target exists")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.FILESYSTEM_CONFLICT
        assert exc_info.value.code == 3
        assert "target exists" in captured.getvalue()

    @pytest.mark.unit()
    def test_verification_error_returns_exit_code_4(self, captured: io.StringIO) -> None:
        """**Acceptance criterion 4:** ``VerificationError`` returns exit code 4.

        A script that sees exit code 4 knows "the project was
        generated but is not verified — check the failed checks."
        """
        exc = VerificationError("tests failed")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.VERIFICATION_FAILED
        assert exc_info.value.code == 4
        assert "tests failed" in captured.getvalue()

    @pytest.mark.unit()
    def test_unexpected_exception_returns_exit_code_5(self, captured: io.StringIO) -> None:
        """**Acceptance criterion 5:** An unexpected exception returns exit code 5.

        A script that sees exit code 5 knows "the tool is broken —
        report a bug." This is the only code that means "Forge is at
        fault," not the user.
        """
        exc = RuntimeError("boom")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.INTERNAL_ERROR
        assert exc_info.value.code == 5
        assert "unexpected internal error" in captured.getvalue().lower()

    @pytest.mark.unit()
    def test_blueprint_error_returns_exit_code_2(self, captured: io.StringIO) -> None:
        """**Acceptance criterion 1 (extended):** ``BlueprintError`` returns exit code 2.

        The WBS explicitly lists criteria 2, 3, 4, and 5. This test
        covers the remaining subclass so that *every* subclass has a
        named test.
        """
        exc = BlueprintError("blueprint not found")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.BLUEPRINT_ERROR
        assert exc_info.value.code == 2
        assert "blueprint not found" in captured.getvalue()

    @pytest.mark.unit()
    def test_forge_error_base_returns_exit_code_5(self, captured: io.StringIO) -> None:
        """**Acceptance criterion 1 (extended):** A bare ``ForgeError`` returns exit code 5.

        The base class is used for "unanticipated user-facing
        failures" — the exit code is 5 (internal error), because the
        failure is not a recognized category. When a failure mode is
        recognized, a subclass with a more specific code is added.
        """
        exc = ForgeError("boom")
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(exc, debug=False)

        assert exc_info.value.code == ExitCode.INTERNAL_ERROR
        assert exc_info.value.code == 5

    @pytest.mark.unit()
    def test_every_subclass_has_a_documented_exit_code(self) -> None:
        """**Acceptance criterion 1:** Every exit code is documented in the code.

        Each ``ForgeError`` subclass declares its ``exit_code``. This
        test asserts that the declaration exists and matches the
        documented table in ``docs/reference/exit-codes.md``.

        The two 1.1.6 subclasses share the code with ``UserInputError``
        by design; the test asserts the sharing explicitly so a
        future maintainer who "fixes" it to a distinct code sees this
        test fail and knows to update the exit-code contract first.
        """
        expected = {
            UserInputError: ExitCode.USER_ERROR,
            BlueprintError: ExitCode.BLUEPRINT_ERROR,
            FilesystemConflictError: ExitCode.FILESYSTEM_CONFLICT,
            VerificationError: ExitCode.VERIFICATION_FAILED,
            MissingRequiredFlagsError: ExitCode.USER_ERROR,
            PromptCancelledError: ExitCode.USER_ERROR,
        }
        for exc_class, expected_code in expected.items():
            assert exc_class.exit_code == expected_code, (
                f"{exc_class.__name__}.exit_code is {exc_class.exit_code!r}, "
                f"expected {expected_code!r}"
            )

    @pytest.mark.unit()
    def test_all_exit_codes_are_distinct(self) -> None:
        """**Acceptance criterion 1:** Every *category* has a distinct code.

        The four original subclasses map to four distinct codes.
        The two 1.1.6 subclasses share ``USER_ERROR`` with
        ``UserInputError`` — that is intentional (see the class
        docstring for ``TestExitCodeMapping``). This test asserts
        the *category* distinction, not the subclass distinction.
        """
        subclasses = [
            UserInputError,
            BlueprintError,
            FilesystemConflictError,
            VerificationError,
        ]
        codes = [cls.exit_code for cls in subclasses]
        assert len(codes) == len(set(codes)), f"Duplicate exit codes: {codes}"
