"""Domain exceptions and exit codes for Forge CLI.

This module is the **single source of truth** for two things:

1. **The exit-code table.** Every failure mode in the CLI maps to one
   of the six ``ExitCode`` values below. Scripts and CI jobs branch
   on these codes, so they are part of the public contract.
2. **The exception hierarchy.** Every user-facing error is a
   ``ForgeError`` subclass. The subclass declares its exit code and
   an optional ``hint``. The CLI's error handler
   (``forge.cli.errors.handle_exception``) dispatches on the
   exception type; it does not parse messages.

Exit code contract
------------------
The exit codes are **part of the public CLI contract**. A script
that runs ``forge create`` and branches on ``$?`` is depending on
the *meaning* of each code. Changing any of them is a breaking
change and requires a major version bump (``0.x`` → ``1.0`` is
exempt from this rule per Semantic Versioning, but the codes are
still expected to be stable within ``0.x``).

The full contract lives in ``docs/reference/exit-codes.md``. The
table below is the authoritative summary; if the two ever
disagree, the doc file wins (and the disagreement is a bug).

+-------+----------------------+-------------------------------------------+
| Code  | Name                 | When it is returned                       |
+=======+======================+===========================================+
| 0     | SUCCESS              | The command completed.                    |
+-------+----------------------+-------------------------------------------+
| 1     | USER_ERROR           | Invalid input, missing argument, or       |
|       |                      | cancellation.                             |
+-------+----------------------+-------------------------------------------+
| 2     | BLUEPRINT_ERROR      | A blueprint is missing, malformed, or     |
|       |                      | incompatible with the installed Forge.    |
+-------+----------------------+-------------------------------------------+
| 3     | FILESYSTEM_CONFLICT  | A target would be overwritten, or a path  |
|       |                      | escapes the permitted root.               |
+-------+----------------------+-------------------------------------------+
| 4     | VERIFICATION_FAILED  | One or more required checks did not pass. |
+-------+----------------------+-------------------------------------------+
| 5     | INTERNAL_ERROR       | An unexpected exception. This is a bug    |
|       |                      | in Forge; the user should report it.      |
+-------+----------------------+-------------------------------------------+

Why these specific codes?
-------------------------
The codes are chosen so that the **first digit** encodes the
severity:

- ``0`` — success.
- ``1`` to ``3`` — the user can fix this without help (bad input,
  missing blueprint, filesystem collision).
- ``4`` — the project was generated but is not verified.
- ``5`` — the tool is broken. Report it.

A script that wants "did the command succeed?" branches on
``== 0``. A script that wants "was it the user's fault?" branches on
``in (1, 2, 3)``. A script that wants "was it a bug?" branches on
``== 5``. This is a common Unix convention, and it is the reason
the codes are not just ``1`` and ``2``.

The ``hint`` attribute
----------------------
Every ``ForgeError`` can carry an optional ``hint`` — a one-line
remediation suggestion. The handler renders the hint on a separate,
dimmed line, so the user sees the *problem* and the *fix* together.

A hint is not required, but it is strongly encouraged. An error
message without a hint is a dead end: the user knows something
failed, but not what to do about it. A hint turns a dead end into a
signpost.

Why the enum lives here, not in ``cli/``
----------------------------------------
The exit code is a **domain decision**, not a CLI decision. Every
``ForgeError`` subclass declares its ``exit_code``; the CLI *uses*
the code (the classifier reads ``exc.exit_code`` and exits with it).
The decision belongs in ``core/``; the use belongs in ``cli/``.

If the enum lived in ``cli/exit_codes.py``, then
``core/exceptions.py`` would import from ``cli/`` — a violation of
the inward dependency rule. The domain would depend on the CLI,
which means the domain could not be tested without the CLI, and
could not be reused in a future web interface.

See ``docs/adr/0005-stabilize-exit-codes-as-a-public-contract.md``
for the full reasoning.

Why the handler raises ``SystemExit``, not ``typer.Exit``
--------------------------------------------------------
The handler is called from ``main()``, *after* Typer's dispatch
has finished. ``typer.Exit`` is only special *inside* a Typer
command. Outside Typer, it propagates into ``sys.exit(main())``,
which treats it as an uncaught exception, prints a traceback, and
exits 1 — not the declared code.

``SystemExit`` is the exception ``sys.exit`` actually expects. The
handler raises it directly, so the process exits with the correct
code, no traceback.

See ``docs/adr/0005-stabilize-exit-codes-as-a-public-contract.md``
for the full reasoning.

Stability
---------
The following are public API and will not change without a major
version bump:

- The ``ExitCode`` values (``0`` through ``5``).
- The ``ForgeError`` attributes (``exit_code``, ``hint``).
- The subclass names (``UserInputError``, ``BlueprintError``,
  ``FilesystemConflictError``, ``VerificationError``).
- The constructor signature
  (``message: str, *, hint: str | None = None``).

Adding a new subclass is a non-breaking change. Removing or
renaming one is breaking. Adding a new attribute to ``ForgeError``
is non-breaking (as long as it has a default). Removing one is
breaking.
"""

from __future__ import annotations

from enum import IntEnum


# -----------------------------------------------------------------------------
# Exit codes
# -----------------------------------------------------------------------------
class ExitCode(IntEnum):
    """Documented exit codes for the ``forge`` command.

    Each member maps to a distinct failure mode. The codes are
    **stable within a minor version**; changing them requires a major
    version bump. See the module docstring for the full table and the
    severity rule.

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

    Subclasses override ``exit_code`` to control the shell exit
    status. The optional ``hint`` is rendered after the error message
    as a dimmed suggestion for how to fix the problem.

    The class is intentionally *not* abstract. A bare ``ForgeError``
    is a valid exception for cases that do not fit one of the
    subclasses yet — it defaults to ``ExitCode.INTERNAL_ERROR``
    (exit 5), which is the correct code for "we did not anticipate
    this failure mode." When a new failure mode is recognized, add a
    subclass with a more specific exit code.

    Attributes:
        exit_code: The shell exit code to use when this error is the
            reason the process exits. **Class-level** — subclasses
            override it (e.g., ``exit_code = ExitCode.USER_ERROR``).
            Read by ``forge.cli.errors.handle_exception``.
        hint: Optional remediation hint shown to the user.
            **Instance-level** — set via the constructor. ``None``
            means "no hint." A subclass may set a class-level default
            hint, but the constructor's ``hint=None`` does not
            clobber it (see ``__init__`` below).
    """

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR
    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Initialize the error.

        Args:
            message: A one-line description of what went wrong.
                Rendered as the first line of the error output.
                Should be imperative and actionable ("project name
                is required"), not vague ("an error occurred").
            hint: An optional one-line remediation suggestion.
                Rendered on a separate, dimmed line. Should tell the
                user *what to do next* ("pass --name my-project"),
                not what they did wrong (the message already says
                that).

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
#
# Each subclass docstring follows the same structure:
#
# - **Exit code:** the code it declares, and the enum member it uses.
# - **When raised:** the failures that produce this subclass.
# - **What the user should do:** the remediation, in one sentence.
# - **Examples:** concrete commands that would raise it.
#
# The structure is deliberate: a reader can scan the four labels
# without reading prose, and a contributor adding a new subclass has
# a template to copy.
class UserInputError(ForgeError):
    """Invalid input from the user.

    **Exit code:** ``ExitCode.USER_ERROR`` (1).

    **When raised:** A missing required flag, an invalid value for a
    flag (e.g., a project name that does not match the pattern), a
    cancellation initiated by the user (Ctrl+C is handled separately
    by the handler, but a "user said no" prompt rejection is a
    ``UserInputError``).

    **What the user should do:** Re-run the command with valid input.
    The message and hint will tell them what is wrong and how to fix
    it.

    **Examples:**

    - ``forge create python.fastapi.api`` without ``--name``.
    - ``forge create python.fastapi.api --name "My Project"``
      (uppercase and spaces).
    - A ``--path`` that does not exist.
    """

    exit_code = ExitCode.USER_ERROR


class BlueprintError(ForgeError):
    """A blueprint is missing, malformed, or unsupported.

    **Exit code:** ``ExitCode.BLUEPRINT_ERROR`` (2).

    **When raised:** The requested blueprint ID is not in the
    registry, the blueprint's ``blueprint.yaml`` does not validate
    against the schema, the blueprint's ``min_forge_version`` is
    newer than the installed Forge.

    **What the user should do:** Choose a different blueprint,
    upgrade Forge, or fix the blueprint. The hint will tell them
    which.

    **Examples:**

    - ``forge create nonexistent.blueprint --name my-api``.
    - ``forge create python.fastapi.api --name my-api`` when the
      blueprint requires Forge 2.0.0 but 1.0.0 is installed.
    """

    exit_code = ExitCode.BLUEPRINT_ERROR


class FilesystemConflictError(ForgeError):
    """A filesystem operation would overwrite or escape the target root.

    **Exit code:** ``ExitCode.FILESYSTEM_CONFLICT`` (3).

    **When raised:** The target directory already exists and
    ``--overwrite`` was not passed; a template path escapes the
    staging directory; a permission error prevents writing to the
    target.

    **What the user should do:** Choose a different ``--output``,
    pass ``--overwrite`` (if they are certain), or fix permissions.
    The hint will tell them which.

    **Examples:**

    - ``forge create python.fastapi.api --name my-api`` when
      ``./my-api`` already exists.
    - ``forge create ... --output /root`` when the user does not
      have write permission on ``/root``.
    """

    exit_code = ExitCode.FILESYSTEM_CONFLICT


class VerificationError(ForgeError):
    """One or more verification checks failed.

    **Exit code:** ``ExitCode.VERIFICATION_FAILED`` (4).

    **When raised:** A required file is missing, the test suite
    fails, the linter reports errors, the Docker build fails.

    **What the user should do:** Address the failed checks. The hint
    will tell them which check failed and how to inspect the
    diagnostic (e.g., ``forge verify --check tests``).

    **Examples:**

    - ``forge verify`` after the tests fail.
    - ``forge verify --check lint`` when ``ruff`` reports errors.
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
