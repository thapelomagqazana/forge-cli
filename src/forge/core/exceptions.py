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

Dual-mode input handling (create command)
-----------------------------------------
The ``create`` command supports two input modes: interactive (a
wizard that asks the user questions) and non-interactive (flags and
defaults, suitable for CI). Two failure modes exist *only* when both
modes are supported, and both are represented here as
``UserInputError`` subclasses:

- ``MissingRequiredFlagsError`` — non-interactive mode, one or more
  required flags absent. Aggregates *all* missing flags into a single
  message so the caller fixes everything in one pass instead of a
  one-error-at-a-time loop.
- ``PromptCancelledError`` — interactive mode, the user pressed
  Ctrl+C or sent EOF at a prompt. Treated as a clean, non-buggy exit.

Both inherit ``ExitCode.USER_ERROR`` (1). They are *not* given their
own exit codes because doing so would be a breaking change to the
public contract (ADR-0005) — and conceptually, both are "the user
needs to do something differently," which is exactly what code 1
means.

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

The ``MissingRequiredFlagsError`` and ``PromptCancelledError``
subclasses added for dual-mode input handling are **new public API**
and are covered by the same stability guarantees as the four
existing subclasses.
"""

from __future__ import annotations

from collections.abc import Sequence
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
# Dual-mode input handling subclasses (Task 1.1.6)
# -----------------------------------------------------------------------------
# These subclasses exist specifically to give the interactive and
# non-interactive paths of the ``create`` command distinct, testable
# failure types. They both map to ``ExitCode.USER_ERROR`` (1) because
# the user can resolve both without help — one by passing flags, the
# other by re-running the wizard.
#
# Keeping them as separate types (rather than raising bare
# ``UserInputError``) is not cosmetic: the CLI error handler can render
# each with a mode-appropriate hint, and tests can assert on the
# specific failure path (``pytest.raises(MissingRequiredFlagsError)``)
# rather than string-matching a message. That's the difference between
# a contract and a suggestion.
class MissingRequiredFlagsError(UserInputError):
    """Non-interactive mode: required flags were not supplied.

    **Exit code:** ``ExitCode.USER_ERROR`` (1), inherited from
    ``UserInputError``.

    **When raised:** ``forge create`` is run without a TTY (stdin is
    not a terminal) and one or more required flags are absent.
    Non-interactive mode cannot prompt, so the only correct behavior
    is to fail fast and explain.

    **What the user should do:** Pass the missing flags, or re-run in
    an interactive terminal. The message lists *every* missing flag,
    not just the first one found, so the caller fixes everything in a
    single edit instead of a painful one-error-at-a-time loop.

    **Examples:**

    - ``forge create python.fastapi.api --no-interactive``
      (missing ``--name``).
    - ``echo "" | forge create`` (stdin is a pipe, not a TTY; all
      required flags missing).

    Attributes:
        command: The command name (e.g., ``"create"``). Used in the
            error message and by callers that want to add
            command-specific guidance.
        missing: The missing flag names, without the ``--`` prefix,
            in stable declaration order. Stored as a tuple so the
            instance stays hashable and immutable — a caught error
            should not be mutable state.
    """

    # Class-level hint so the user always sees a remediation line,
    # even when the raiser does not pass one. The constructor's
    # ``hint=None`` default will not clobber this (see ForgeError.__init__).
    hint = "Pass the missing flags, or re-run in an interactive terminal."

    def __init__(
        self,
        command: str,
        missing: Sequence[str],
        *,
        hint: str | None = None,
    ) -> None:
        """Initialize the error with the aggregate list of missing flags.

        Args:
            command: The command name (e.g., ``"create"``). Rendered
                in the message so the user knows which command needs
                fixing.
            missing: The missing flag names, without the ``--``
                prefix. Passed as any sequence so callers can
                forward a list comprehension directly; normalized to
                an immutable tuple here so the instance is safe to
                pass across threads and store in test fixtures.
            hint: Override the default hint. Most callers should not
                pass this — the class-level hint is already correct
                for every known caller.

        The ``missing`` parameter is positional-only in spirit (no
        keyword marker before it) because constructing the error is
        always a two-argument operation: ``(command, missing)``. The
        ``hint`` is keyword-only for the same reason as in
        ``ForgeError``.
        """
        # Normalize to a tuple so the public attribute is stable.
        # A caller that passes a generator (e.g., ``(f for f in ...)``)
        # would otherwise be able to consume it exactly once, which is
        # a subtle footgun. Materializing here makes the attribute
        # safe to read in error handlers, loggers, and tests.
        self.command = command
        self.missing: tuple[str, ...] = tuple(missing)

        # Render the flags with the ``--`` prefix and join with
        # commas so the message is copy-pasteable. Using ", " as the
        # separator (rather than "and") keeps the message short even
        # with many missing flags and reads naturally in CI logs.
        formatted = ", ".join(f"--{flag}" for flag in self.missing)
        message = f"Missing required option(s) for non-interactive mode: " f"{formatted}."

        super().__init__(message, hint=hint)


class PromptCancelledError(UserInputError):
    """Interactive mode: the user aborted a prompt.

    **Exit code:** ``ExitCode.USER_ERROR`` (1), inherited from
    ``UserInputError``.

    **When raised:** The user pressed Ctrl+C or sent EOF at a
    questionary prompt. This is a *clean* cancellation, not a bug:
    the user exercised their right to stop, so the process should
    exit quietly with a short message and no traceback.

    **What the user should do:** Re-run the command when ready. The
    hint reminds them that ``--no-interactive`` plus flags is
    available if they prefer.

    **Examples:**

    - ``forge create python.fastapi.api`` (interactive), user hits
      Ctrl+C at the project-name prompt.

    Attributes:
        prompt: The prompt message that was interrupted, if known.
            Included in the error message so the user can see *where*
            they cancelled, which matters for multi-step wizards.
            ``None`` means "the interruption happened at a step we
            cannot identify" (e.g., between prompts).
    """

    hint = "Re-run when ready, or use --no-interactive with explicit flags " "for scriptable usage."

    def __init__(
        self,
        prompt: str | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        """Initialize the cancellation error.

        Args:
            prompt: The prompt message that was interrupted. Optional
                because the interruption may occur between prompts
                (e.g., during a select menu that has no single
                message). When provided, the message includes it so
                the user knows which step they stopped at.
            hint: Override the default hint. Most callers should not
                pass this — the class-level hint already covers both
                "re-run" and "switch to non-interactive."
        """
        self.prompt = prompt
        if prompt:
            message = f"Interactive prompt cancelled by user at '{prompt}'."
        else:
            message = "Interactive prompt cancelled by user."
        super().__init__(message, hint=hint)


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
    "MissingRequiredFlagsError",
    "PromptCancelledError",
    "UserInputError",
    "VerificationError",
]
