"""Unit tests for ``forge.cli.ui``.

This module is the CLI's *voice*: every user-facing message goes
through one of its helpers. The tests assert on three things:

1. **Stream routing.** Success and info go to stdout; warnings and
   errors go to stderr. This is the Unix contract — a script that
   pipes ``forge list --json`` to ``jq`` must not receive error
   messages mixed in.
2. **Prefixes.** Every helper renders with a consistent prefix
   (``✓``, ``⚠``, ``✗``, ``Hint:``). Plain info has no prefix.
3. **Broken-pipe safety.** A helper that writes to a closed pipe
   must not raise. This is a normal condition (``forge list | head``),
   not an error.

The tests use an in-memory ``Console`` for each stream rather than
``capsys``, because Rich's rendering pipeline is not captured
reliably by ``capsys`` across platforms. The fixture replaces the
module-level consoles with ``Console(file=StringIO())`` instances,
so the helpers write to a buffer the test can inspect directly.
"""

from __future__ import annotations

import io
from collections.abc import Callable

import pytest
from rich.console import Console

from forge.cli import ui


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> tuple[io.StringIO, io.StringIO]:
    """Replace the module-level consoles with in-memory ones.

    Returns a tuple ``(stdout_buf, stderr_buf)``. Both are
    ``io.StringIO`` instances that the test can read with
    ``.getvalue()``.

    The fixture is *autouse* per-test (not module-scoped), so each
    test starts with a clean buffer and cannot leak output from a
    previous test.

    The consoles are constructed with ``force_terminal=False`` so
    that Rich does not emit ANSI colour codes. This makes the
    assertions on the buffer text straightforward — we can assert
    on ``"✓"``, not on ``"\x1b[32m✓\x1b[0m"``. A separate test
    covers the colour path.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    stdout_console = Console(
        file=stdout_buf,
        highlight=False,
        force_terminal=False,
        soft_wrap=False,
    )
    stderr_console = Console(
        file=stderr_buf,
        stderr=True,
        highlight=False,
        force_terminal=False,
        soft_wrap=False,
    )
    monkeypatch.setattr(ui, "_stdout_console", stdout_console)
    monkeypatch.setattr(ui, "_stderr_console", stderr_console)
    return stdout_buf, stderr_buf


# -----------------------------------------------------------------------------
# print_success
# -----------------------------------------------------------------------------
class TestPrintSuccess:
    """Tests for ``ui.print_success``."""

    @pytest.mark.unit()
    def test_writes_to_stdout(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Success goes to stdout, not stderr."""
        stdout, stderr = captured
        ui.print_success("all good")
        assert "all good" in stdout.getvalue()
        assert "all good" not in stderr.getvalue()

    @pytest.mark.unit()
    def test_includes_checkmark_prefix(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Success renders with a ✓ prefix."""
        stdout, _ = captured
        ui.print_success("done")
        assert "✓" in stdout.getvalue()

    @pytest.mark.unit()
    def test_message_follows_prefix(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """The message follows the prefix on the same line."""
        stdout, _ = captured
        ui.print_success("done")
        assert "✓ done" in stdout.getvalue().strip()


# -----------------------------------------------------------------------------
# print_warning
# -----------------------------------------------------------------------------
class TestPrintWarning:
    """Tests for ``ui.print_warning``."""

    @pytest.mark.unit()
    def test_writes_to_stderr(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Warning goes to stderr, not stdout.

        Warnings are commentary, not product. They must not pollute
        the machine-readable output stream.
        """
        stdout, stderr = captured
        ui.print_warning("heads up")
        assert "heads up" in stderr.getvalue()
        assert "heads up" not in stdout.getvalue()

    @pytest.mark.unit()
    def test_includes_warning_prefix(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Warning renders with a ⚠ prefix."""
        _, stderr = captured
        ui.print_warning("watch out")
        assert "⚠" in stderr.getvalue()


# -----------------------------------------------------------------------------
# print_error
# -----------------------------------------------------------------------------
class TestPrintError:
    """Tests for ``ui.print_error``."""

    @pytest.mark.unit()
    def test_writes_to_stderr(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Error goes to stderr, not stdout.

        This is the single most important test in this file. If
        ``print_error`` wrote to stdout, ``forge verify --json``
        would emit invalid JSON on the failure path, breaking every
        script that parses it.
        """
        stdout, stderr = captured
        ui.print_error("something broke")
        assert "something broke" in stderr.getvalue()
        assert "something broke" not in stdout.getvalue()

    @pytest.mark.unit()
    def test_includes_error_prefix(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Error renders with a ✗ prefix."""
        _, stderr = captured
        ui.print_error("boom")
        assert "✗" in stderr.getvalue()

    @pytest.mark.unit()
    def test_multiline_message_preserved(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """A multi-line message is preserved as-is."""
        _, stderr = captured
        ui.print_error("line one\nline two")
        output = stderr.getvalue()
        assert "line one" in output
        assert "line two" in output


# -----------------------------------------------------------------------------
# print_info
# -----------------------------------------------------------------------------
class TestPrintInfo:
    """Tests for ``ui.print_info``."""

    @pytest.mark.unit()
    def test_writes_to_stdout(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Info goes to stdout."""
        stdout, stderr = captured
        ui.print_info("for your information")
        assert "for your information" in stdout.getvalue()
        assert "for your information" not in stderr.getvalue()

    @pytest.mark.unit()
    def test_has_no_prefix(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Info renders as plain text, with no prefix."""
        stdout, _ = captured
        ui.print_info("plain")
        # The buffer contains exactly "plain\n" — no prefix characters.
        assert stdout.getvalue() == "plain\n"


# -----------------------------------------------------------------------------
# print_hint
# -----------------------------------------------------------------------------
class TestPrintHint:
    """Tests for ``ui.print_hint``."""

    @pytest.mark.unit()
    def test_writes_to_stderr(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Hint goes to stderr (it accompanies an error)."""
        stdout, stderr = captured
        ui.print_hint("try --help")
        assert "try --help" in stderr.getvalue()
        assert "try --help" not in stdout.getvalue()

    @pytest.mark.unit()
    def test_includes_hint_prefix(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Hint renders with a ``Hint:`` prefix."""
        _, stderr = captured
        ui.print_hint("try --help")
        assert "Hint:" in stderr.getvalue()
        assert "try --help" in stderr.getvalue()


# -----------------------------------------------------------------------------
# Broken-pipe fallback
# -----------------------------------------------------------------------------
class TestFallback:
    """Tests for ``typer_echo_safe``, the broken-pipe fallback.

    The fallback is what keeps the CLI from crashing when the
    consumer closes the pipe (e.g., ``forge list | head``). These
    tests exercise the fallback directly, because simulating a real
    broken pipe in a unit test is fragile. The integration test for
    ``forge list | head`` (in a later task) exercises the real path.
    """

    @pytest.mark.unit()
    def test_does_not_raise_on_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``typer_echo_safe`` writes to stdout without raising."""
        ui.typer_echo_safe("hello")
        captured = capsys.readouterr()
        assert "hello" in captured.out
        assert captured.err == ""

    @pytest.mark.unit()
    def test_does_not_raise_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``typer_echo_safe(err=True)`` writes to stderr."""
        ui.typer_echo_safe("oops", err=True)
        captured = capsys.readouterr()
        assert "oops" in captured.err
        assert captured.out == ""

    @pytest.mark.unit()
    def test_does_not_raise_on_broken_pipe(self) -> None:
        """``typer_echo_safe`` catches ``BrokenPipeError`` from ``typer.echo``.

        We monkeypatch ``typer.echo`` to raise ``BrokenPipeError``,
        which is what a real broken pipe looks like. The fallback
        must catch it and return normally. A helper that raised here
        would crash the CLI on ``forge list | head``.
        """
        import typer

        original_echo = typer.echo

        def raising_echo(*args: object, **kwargs: object) -> None:
            raise BrokenPipeError()

        typer.echo = raising_echo  # type: ignore[assignment]
        try:
            # Must not raise.
            ui.typer_echo_safe("hello")
        finally:
            typer.echo = original_echo  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Colour handling
# -----------------------------------------------------------------------------
class TestColour:
    """Tests for the ``NO_COLOR`` / ``FORCE_COLOR`` contract.

    The helpers do not emit colour codes themselves — Rich does. The
    tests below verify that ``_is_colour_enabled`` reflects the
    environment, and that the actual output is readable in plain
    text (with the prefix as the signal, not the colour).
    """

    @pytest.mark.unit()
    def test_is_colour_enabled_false_when_no_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``NO_COLOR`` disables colour."""
        monkeypatch.setenv("NO_COLOR", "1")
        assert ui._is_colour_enabled() is False

    @pytest.mark.unit()
    def test_is_colour_enabled_true_when_force_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``FORCE_COLOR`` enables colour."""
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert ui._is_colour_enabled() is True

    @pytest.mark.unit()
    def test_no_color_does_not_affect_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured: tuple[io.StringIO, io.StringIO],
    ) -> None:
        """With ``NO_COLOR`` set, the message is still readable."""
        monkeypatch.setenv("NO_COLOR", "1")
        stdout, _ = captured
        ui.print_success("done")
        # The prefix and message are both present, regardless of colour.
        assert "✓" in stdout.getvalue()
        assert "done" in stdout.getvalue()

    @pytest.mark.unit()
    def test_force_color_emits_ansi_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With ``FORCE_COLOR`` set and a terminal, ANSI codes are emitted.

        This test constructs a *fresh* Console with ``force_terminal=True``
        to prove that Rich respects the setting. The module-level
        consoles are replaced by the ``captured`` fixture with
        ``force_terminal=False``; we do not use them here, because
        this test is about Rich's behaviour, not the helper's.
        """
        monkeypatch.setenv("FORCE_COLOR", "1")
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, highlight=False)
        console.print("[green]✓[/green] done")
        output = buf.getvalue()
        # The green colour code is present.
        assert "\x1b[32m" in output
        # And the text is still readable.
        assert "✓" in output
        assert "done" in output


class TestBrokenPipeFallback:
    """Tests for each helper's broken-pipe fallback.

    The helpers catch ``BrokenPipeError`` from Rich and fall back to
    ``typer.echo``. This is what keeps ``forge list | head`` from
    crashing when ``head`` closes the pipe. The fallback is a
    defensive path — every helper has one — but it is not exercised
    by the happy-path tests. These tests exercise it directly.
    """

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        ("helper", "args"),
        [
            (ui.print_success, ("done",)),
            (ui.print_warning, ("watch out",)),
            (ui.print_error, ("boom",)),
            (ui.print_info, ("info",)),
            (ui.print_hint, ("try --help",)),
            (ui.print_cancelled, ()),
        ],
    )
    def test_helper_falls_back_on_broken_pipe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        helper: Callable[..., None],
        args: tuple[str, ...],
    ) -> None:
        """Each helper catches ``BrokenPipeError`` and falls back to ``typer.echo``.

        The console is replaced with a mock whose ``print`` method
        raises ``BrokenPipeError``, which is what a real broken pipe
        looks like. The helper must catch it and call
        ``typer_echo_safe`` (which writes the message via
        ``typer.echo``). The test asserts the message reaches the
        fallback stream.
        """
        from unittest.mock import MagicMock

        broken_console = MagicMock()
        broken_console.print.side_effect = BrokenPipeError()
        monkeypatch.setattr(ui, "_stdout_console", broken_console)
        monkeypatch.setattr(ui, "_stderr_console", broken_console)

        # Must not raise.
        helper(*args)

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        if args:
            assert args[0] in combined


class TestPrintCancelled:
    """Tests for ``ui.print_cancelled``."""

    @pytest.mark.unit()
    def test_writes_to_stderr(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Cancellation goes to stderr, not stdout."""
        stdout, stderr = captured
        ui.print_cancelled()
        assert "Cancelled" in stderr.getvalue()
        assert "Cancelled" not in stdout.getvalue()

    @pytest.mark.unit()
    def test_default_message(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """The default message is ``Cancelled.``."""
        _, stderr = captured
        ui.print_cancelled()
        assert "Cancelled." in stderr.getvalue()

    @pytest.mark.unit()
    def test_custom_message(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """A custom message is rendered verbatim."""
        _, stderr = captured
        ui.print_cancelled("Aborted by user.")
        assert "Aborted by user." in stderr.getvalue()

    @pytest.mark.unit()
    def test_no_error_prefix(self, captured: tuple[io.StringIO, io.StringIO]) -> None:
        """Cancellation does not use the "Error:" prefix.

        Ctrl+C is a cancellation, not an error. The user does not
        need to be told it was an error.
        """
        _, stderr = captured
        ui.print_cancelled()
        assert "Error:" not in stderr.getvalue()
