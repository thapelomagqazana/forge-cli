"""Domain exceptions and exit codes for Forge CLI.

This module is the single source of truth for two things:

1. **The exit-code table.** Every failure mode in the CLI maps to one
   of the six ``ExitCode`` values below. Scripts and CI jobs branch on
   these codes, so they are part of the public contract.
2. **The exception hierarchy.** Every user-facing error is a
   ``ForgeError`` subclass. The subclass declares its exit code and an
   optional ``hint``. The CLI's error handler
   (``forge.cli.errors.handle_exception``) dispatches on the exception
   type; it does not parse messages.

Exit code contract
------------------
The exit codes below are **part of the public CLI contract**. Changing
any of them is a breaking change and requires a major version bump
(``0.x`` → ``1.0`` is exempt from this rule, per Semantic Versioning,
but the codes are still expected to be stable within ``0.x``).

- ``0`` — Success. The command did what the user asked.
- ``1`` — User error. Invalid input, missing argument, cancellation.
- ``2`` — Blueprint error. A blueprint is missing, malformed, or
  incompatible with the installed Forge.
- ``3`` — Filesystem conflict. A target would be overwritten, or a
  path escapes the permitted root.
- ``4`` — Verification failure. One or more required checks did not
  pass.
- ``5`` — Internal error. An unexpected exception. This is a bug in
  Forge; the user should report it.

Why these specific codes?
-------------------------
The codes are deliberately chosen so that the *first digit* of the
code encodes the *severity* (0 = success, 1, 2, 3 = user-correctable,
4 = verification, 5 = bug). A script that wants "did the command
succeed?" branches on ``== 0``. A script that wants "was it the
user's fault?" branches on ``in (1, 2, 3)``. A script that wants
"was it a bug?" branches on ``== 5``. This is a common Unix
convention and it is the reason the codes are not just ``1`` and
``2``.

The ``hint`` attribute
----------------------
Every ``ForgeError`` can carry an optional ``hint`` — a one-line
remediation suggestion. The handler renders the hint on a separate,
dimmed line, so the user sees the *problem* and the *fix* together.

A hint is not required, but it is strongly encouraged. An error
message without a hint is a dead end: the user knows something
failed, but not what to do about it. A hint turns a dead end into
a signpost.

Stability
---------
The following are public API and will not change without a major
version bump:

- The ``ExitCode`` values (``0`` through ``5``).
- The ``ForgeError`` attributes (``exit_code``, ``hint``).
- The subclass names (``UserInputError``, ``BlueprintError``,
  ``FilesystemConflictError``, ``VerificationError``).
- The constructor signature (``message: str, *, hint: str | None = None``).

Adding a new subclass is a non-breaking change. Removing or renaming
one is breaking. Adding a new attribute to ``ForgeError`` is
non-breaking (as long as it has a default). Removing one is breaking.
"""

from __future__ import annotations

from enum import IntEnum


# -----------------------------------------------------------------------------
# Exit codes
# -----------------------------------------------------------------------------
class ExitCode(IntEnum):
    """Documented exit codes for the ``forge`` command.

    ``IntEnum`` is used (rather than ``Enum``) so the values compare
    equal to plain integers. This matters for two reasons:

    1. ``sys.exit(ExitCode.SUCCESS)`` works — Python's ``sys.exit``
       accepts an int or ``None``, and an ``IntEnum`` member *is* an
       ``int``.
    2. ``result.exit_code == 0`` works in tests — pytest's
       ``CliRunner`` returns a plain ``int``, and comparing an
       ``IntEnum`` member to an ``int`` is well-defined.

    With a plain ``Enum``, both of those would require an explicit
    ``.value`` access, which is easy to forget.
    """

    SUCCESS = 0
    USER_ERROR = 1
    BLUEPRINT_ERROR = 2
    FILESYSTEM_CONFLICT = 3
    VERIFICATION_FAILED = 4
    INTERNAL_ERROR = 5


# -----------------------------------------------------------------------------
# Base class
# -----------------------------------------------------------------------------
class ForgeError(Exception):
    """Base class for every user-facing Forge error.

    Subclasses override ``exit_code`` to control the shell exit status.
    The optional ``hint`` is rendered after the error message as a
    dimmed suggestion for how to fix the problem.

    The class is intentionally *not* abstract. A bare ``ForgeError``
    is a valid exception for cases that do not fit one of the
    subclasses yet — it defaults to ``ExitCode.INTERNAL_ERROR`` (exit
    5), which is the correct code for "we did not anticipate this
    failure mode." When a new failure mode is recognized, add a
    subclass with a more specific exit code.

    Attributes:
        exit_code: The shell exit code to use when this error is the
            reason the process exits. Class-level; subclasses override
            it. Read by ``forge.cli.errors.handle_exception``.
        hint: Optional remediation hint shown to the user. Instance-level;
            set via the constructor. ``None`` means "no hint."
    """

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR
    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Initialize the error.

        Args:
            message: A one-line description of what went wrong. Rendered
                as the first line of the error output. Should be
                imperative and actionable ("project name is required"),
                not vague ("an error occurred").
            hint: An optional one-line remediation suggestion. Rendered
                on a separate, dimmed line. Should tell the user *what
                to do next* ("pass --name my-project"), not what they
                did wrong (the message already says that).

        The ``hint`` parameter is keyword-only (``*``) to prevent the
        common mistake of passing it positionally:
        ``ForgeError("msg", "hint")`` — which would silently pass the
        hint as a positional argument and be rejected. Keyword-only
        forces ``ForgeError("msg", hint="hint")``, which is
        self-documenting.
        """
        super().__init__(message)
        # Only assign if the caller actually passed a hint. This keeps
        # the class-level default (``None``) in place when the caller
        # does not pass one, so ``is None`` checks and ``if exc.hint:``
        # both work correctly. If we assigned unconditionally, a
        # subclass that set a class-level default hint would be
        # clobbered by ``None``.
        if hint is not None:
            self.hint = hint


# -----------------------------------------------------------------------------
# Subclasses
# -----------------------------------------------------------------------------
# Each subclass declares a distinct exit code. The handler dispatches
# on the type; it does not parse the message. Adding a subclass is the
# only supported way to add a new exit code.
class UserInputError(ForgeError):
    """Invalid input from the user.

    Examples: a missing required flag, an invalid value for a flag
    (e.g., a project name that does not match the pattern), a
    cancellation initiated by the user (Ctrl+C is handled separately
    by the handler, but a "user said no" prompt rejection is a
    ``UserInputError``).

    The user can fix this without help. The hint should tell them how.
    """

    exit_code = ExitCode.USER_ERROR


class BlueprintError(ForgeError):
    """A blueprint is missing, malformed, or unsupported.

    Examples: the requested blueprint ID is not in the registry, the
    blueprint's ``blueprint.yaml`` does not validate against the
    schema, the blueprint's ``min_forge_version`` is newer than the
    installed Forge.

    The user can usually fix this by choosing a different blueprint,
    installing a newer Forge, or fixing the blueprint. The hint
    should tell them which.
    """

    exit_code = ExitCode.BLUEPRINT_ERROR


class FilesystemConflictError(ForgeError):
    """A filesystem operation would overwrite or escape the target root.

    Examples: the target directory already exists and ``--overwrite``
    was not passed; a template path escapes the staging directory; a
    permission error prevents writing to the target.

    The user can fix this by choosing a different ``--output``,
    passing ``--overwrite`` (if they are certain), or fixing
    permissions. The hint should tell them which.
    """

    exit_code = ExitCode.FILESYSTEM_CONFLICT


class VerificationError(ForgeError):
    """One or more verification checks failed.

    Examples: a required file is missing, the test suite fails, the
    linter reports errors, the Docker build fails.

    The user can fix this by addressing the failed checks. The hint
    should tell them which check failed and how to inspect the
    diagnostic (e.g., ``forge verify --check tests``).
    """

    exit_code = ExitCode.VERIFICATION_FAILED


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
# Every name in this module is part of the public API except the
# ``_``-prefixed implementation details (there are none). The
# ``__all__`` list is the contract: adding to it is non-breaking,
# removing from it is breaking.
__all__ = [
    "BlueprintError",
    "ExitCode",
    "FilesystemConflictError",
    "ForgeError",
    "UserInputError",
    "VerificationError",
]
