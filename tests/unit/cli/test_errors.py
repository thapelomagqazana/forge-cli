"""Unit tests for ``forge.cli.errors.handle_exception``.

The handler is the single most security-sensitive function in the
CLI. It decides what the user sees when something goes wrong. Its
contract has *four clauses*, one per category of exception:

1. **``ForgeError`` renders a message + hint, exit with code.**
   The message is the first line; the hint (if present) is a
   separate, dimmed line. The exit code is the subclass's declared
   ``exit_code``. ``--debug`` does *not* change this behaviour.

2. **``click.ClickException`` renders its message, exit with
   Click's code (usually 2).** These are user errors from Click's
   parser: the user typed a command that does not exist, or passed
   an argument that does not match the signature. ``--debug`` does
   *not* change this behaviour.

3. **Cancellations (``KeyboardInterrupt``, ``typer.Abort``) render
   "Cancelled.", exit 1.** No traceback. No "Error:" prefix. A
   single dimmed line on stderr. ``--debug`` does *not* change this
   behaviour.

4. **Unexpected exceptions render a generic message, exit 5.**
   The message must not include the exception's class name,
   message, or traceback. Those are internal details that could
   hint at a vulnerability. The user gets a link to the issue
   tracker. **Under ``--debug``, the handler re-raises the
   exception instead**, so the developer sees the traceback.

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
assert on all four clauses in isolation.
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
        stdout buffer is available via ``captured_stdout`` for tests
        that need to assert it was not written to.
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
# Clause 2: Click exceptions render friendly, exit with Click's code
# -----------------------------------------------------------------------------
class TestClickException:
    """Tests for the ``click.ClickException`` branch of the handler.

    Click raises ``ClickException`` (``UsageError``, ``BadParameter``,
    ``Abort``, etc.) when the user's input does not match the
    command's contract. These are user errors, not bugs. They must be
    rendered friendly with Click's own exit code (2 for usage errors),
    *even under* ``--debug``.
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
# Clause 3: KeyboardInterrupt / typer.Abort render "Cancelled.", exit 1
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

    The parametrize lists include every category of exception:

    - ``ForgeError`` and its four subclasses.
    - ``click.ClickException`` (``UsageError``).
    - Unexpected exceptions: ``RuntimeError``, ``ValueError``,
      ``KeyError``, ``OSError``, and the bare ``Exception``.
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
            click.UsageError("no such command"),
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
            click.UsageError("no such command"),
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
    """

    @pytest.mark.unit()
    def test_all_subclasses_have_distinct_codes(self) -> None:
        """Every ``ForgeError`` subclass has a distinct exit code."""
        subclasses = [
            UserInputError,
            BlueprintError,
            FilesystemConflictError,
            VerificationError,
        ]
        codes = [cls.exit_code for cls in subclasses]
        assert len(codes) == len(set(codes)), f"Duplicate exit codes: {codes}"

    @pytest.mark.unit()
    def test_base_class_uses_internal_error(self) -> None:
        """A bare ``ForgeError`` uses the internal-error code."""
        assert ForgeError.exit_code == ExitCode.INTERNAL_ERROR
