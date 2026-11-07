"""Unit tests for the domain exception hierarchy.

Focused on the two subclasses added for dual-mode input handling:

- ``MissingRequiredFlagsError`` — non-interactive mode, missing flags.
- ``PromptCancelledError`` — interactive mode, user cancelled.

The existing four subclasses (``UserInputError``, ``BlueprintError``,
``FilesystemConflictError``, ``VerificationError``) are tested by the
contract tests that assert on exit codes; this file adds the tests that
the new subclasses need for their *own* behavior (message formatting,
attribute immutability, hint rendering).
"""

from __future__ import annotations

import pytest

from forge.core.exceptions import (
    ExitCode,
    ForgeError,
    MissingRequiredFlagsError,
    PromptCancelledError,
    UserInputError,
)


# -----------------------------------------------------------------------------
# MissingRequiredFlagsError
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_missing_required_flags_error_is_a_user_input_error() -> None:
    """The new subclass must inherit the exit-code contract.

    ``handle_exception`` dispatches on ``isinstance(exc, ForgeError)``;
    the exit code is read from the class attribute. Asserting the
    inheritance here catches a future refactor that accidentally
    reparents the class to ``Exception`` (which would surface as an
    "unexpected internal error" in production).
    """
    exc = MissingRequiredFlagsError(command="create", missing=("name",))
    assert isinstance(exc, UserInputError)
    assert isinstance(exc, ForgeError)
    assert exc.exit_code == ExitCode.USER_ERROR


@pytest.mark.unit()
def test_missing_required_flags_error_lists_every_missing_flag() -> None:
    """The message must list *all* missing flags, not just the first.

    This is the "fail fast, fail complete" contract for scripted
    callers: fixing two flags in one edit beats fixing them one
    command-run at a time.
    """
    exc = MissingRequiredFlagsError(command="create", missing=("name", "blueprint"))
    message = str(exc)
    assert "--name" in message
    assert "--blueprint" in message
    # The message must name the mode so the user knows *why* the
    # flags are required (they might be in a terminal and forgot
    # --no-interactive).
    assert "non-interactive" in message.lower()


@pytest.mark.unit()
def test_missing_required_flags_error_formats_flags_with_double_dash() -> None:
    """Flags render with ``--`` prefix so they are copy-pasteable.

    A message that says ``name, blueprint`` sends the user back to
    ``--help``. ``--name, --blueprint`` sends them back to their
    command line.
    """
    exc = MissingRequiredFlagsError(command="create", missing=("name",))
    assert "--name" in str(exc)
    # And not the bare form, which would be confusing in a shell.
    assert "name," not in str(exc).replace("--name,", "")


@pytest.mark.unit()
def test_missing_required_flags_error_exposes_immutable_missing_tuple() -> None:
    """``missing`` is a tuple, not the caller's list.

    The attribute is public API. A caller that passes a list and then
    mutates it should not be able to mutate the error after it has
    been caught — that would make the error non-deterministic across
    handlers.
    """
    caller_list = ["name"]
    exc = MissingRequiredFlagsError(command="create", missing=caller_list)
    assert isinstance(exc.missing, tuple)
    assert exc.missing == ("name",)


@pytest.mark.unit()
def test_missing_required_flags_error_has_default_hint() -> None:
    """Every user-facing error carries a hint (dead-end rule)."""
    exc = MissingRequiredFlagsError(command="create", missing=("name",))
    assert exc.hint is not None
    assert "interactive" in exc.hint.lower()


@pytest.mark.unit()
def test_missing_required_flags_error_accepts_hint_override() -> None:
    """Callers with better context can override the default hint."""
    exc = MissingRequiredFlagsError(
        command="create",
        missing=("name",),
        hint="Try --name my-project.",
    )
    assert exc.hint == "Try --name my-project."


# -----------------------------------------------------------------------------
# PromptCancelledError
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_prompt_cancelled_error_is_a_user_input_error() -> None:
    """Cancellation is a user-fault exit, not an internal error."""
    exc = PromptCancelledError()
    assert isinstance(exc, UserInputError)
    assert exc.exit_code == ExitCode.USER_ERROR


@pytest.mark.unit()
def test_prompt_cancelled_error_without_prompt_has_generic_message() -> None:
    """When the prompt is unknown (e.g., between prompts), say so."""
    exc = PromptCancelledError()
    message = str(exc)
    assert "cancelled" in message.lower()
    # No dangling "at ''" or "at None" — the message must read
    # naturally when no prompt is given.
    assert "''" not in message
    assert "None" not in message


@pytest.mark.unit()
def test_prompt_cancelled_error_with_prompt_names_the_step() -> None:
    """When the prompt is known, include it so the user knows where.

    A wizard has multiple steps; "cancelled" without context is a
    dead end when the user does not remember which step they were
    on. Naming the step is the difference between a useful message
    and a shrug.
    """
    exc = PromptCancelledError(prompt="Project name")
    assert "Project name" in str(exc)


@pytest.mark.unit()
def test_prompt_cancelled_error_has_default_hint() -> None:
    """The hint must mention both remediation paths (re-run, flags)."""
    exc = PromptCancelledError()
    assert exc.hint is not None
    hint = exc.hint.lower()
    assert "re-run" in hint or "re run" in hint
    assert "--no-interactive" in exc.hint


@pytest.mark.unit()
def test_prompt_cancelled_error_accepts_hint_override() -> None:
    """Callers with better context can override the default hint."""
    exc = PromptCancelledError(hint="Use --no-interactive for scripts.")
    assert exc.hint == "Use --no-interactive for scripts."
