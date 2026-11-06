"""Domain exceptions and exit codes for Forge CLI.

Every error that can be rendered to the user should be a ``ForgeError``
subclass with a stable ``exit_code``. This keeps the mapping between
"what went wrong" and "what exit code the shell sees" in one place.

Exit code contract
------------------
The exit codes below are part of the public CLI contract. Changing any
of them is a breaking change and requires a major version bump.

- ``0`` — Success.
- ``1`` — User error (invalid input, missing argument).
- ``2`` — Blueprint error (invalid or missing blueprint).
- ``3`` — Filesystem conflict (collision, permission denied).
- ``4`` — Verification failure (one or more checks failed).
- ``5`` — Internal error (unexpected exception).
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Documented exit codes for the ``forge`` command."""

    SUCCESS = 0
    USER_ERROR = 1
    BLUEPRINT_ERROR = 2
    FILESYSTEM_CONFLICT = 3
    VERIFICATION_FAILED = 4
    INTERNAL_ERROR = 5


class ForgeError(Exception):
    """Base class for every user-facing Forge error.

    Subclasses override ``exit_code`` to control the shell exit status.
    The optional ``hint`` is rendered after the error message as a
    dimmed suggestion for how to fix the problem.

    Attributes:
        exit_code: The shell exit code to use when this error is the
            reason the process exits.
        hint: Optional remediation hint shown to the user.
    """

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR
    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class UserInputError(ForgeError):
    """Invalid input from the user (bad flag, missing value, etc.)."""

    exit_code = ExitCode.USER_ERROR


class BlueprintError(ForgeError):
    """A blueprint is missing, malformed, or unsupported."""

    exit_code = ExitCode.BLUEPRINT_ERROR


class FilesystemConflictError(ForgeError):
    """A filesystem operation would overwrite or escape the target root."""

    exit_code = ExitCode.FILESYSTEM_CONFLICT


class VerificationError(ForgeError):
    """One or more verification checks failed."""

    exit_code = ExitCode.VERIFICATION_FAILED
