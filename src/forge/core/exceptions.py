"""Domain exceptions and exit codes for Forge CLI.

This module is the **single source of truth** for two things:

1. **The exit-code table.** Every failure mode in the CLI maps to one
   of the ``ExitCode`` values below. Scripts and CI jobs branch on
   these codes, so they are part of the public contract.
2. **The exception hierarchy.** Every user-facing error is a
   ``ForgeError`` subclass. The subclass declares its exit code and
   an optional ``hint``. The CLI's error handler
   (``forge.cli.errors.handle_exception``) dispatches on the
   exception type; it does not parse messages.

Exit code contract
------------------
The exit codes are **part of the public CLI contract**. A script that
runs ``forge create`` and branches on ``$?`` is depending on the
*meaning* of each code. Changing any of them is a breaking change and
requires a major version bump.

+-------+-----------------------+-------------------------------------------+
| Code  | Name                  | When it is returned                       |
+=======+=======================+===========================================+
| 0     | SUCCESS               | The command completed.                    |
+-------+-----------------------+-------------------------------------------+
| 1     | USER_ERROR            | Invalid input, missing argument, or       |
|       |                       | cancellation.                             |
+-------+-----------------------+-------------------------------------------+
| 2     | BLUEPRINT_ERROR       | A blueprint is missing, malformed, or     |
|       |                       | incompatible with the installed Forge.    |
+-------+-----------------------+-------------------------------------------+
| 3     | FILESYSTEM_CONFLICT   | A target would be overwritten, or a path  |
|       |                       | escapes the permitted root.               |
+-------+-----------------------+-------------------------------------------+
| 4     | VERIFICATION_FAILED   | One or more required checks did not pass. |
+-------+-----------------------+-------------------------------------------+
| 5     | INTERNAL_ERROR        | An unexpected exception. This is a bug    |
|       |                       | in Forge; the user should report it.      |
+-------+-----------------------+-------------------------------------------+
| 6     | COLLISION             | A planned **write** would replace an      |
|       |                       | existing file and ``--overwrite`` was not |
|       |                       | passed. The write was refused; nothing    |
|       |                       | was modified.                             |
+-------+-----------------------+-------------------------------------------+
| 7     | DRY_RUN_COLLISIONS    | A **dry-run** produced a plan that        |
|       |                       | contains collisions. The plan was         |
|       |                       | emitted; **nothing was written.**         |
+-------+-----------------------+-------------------------------------------+

Note on 6 vs 7
--------------
Both codes signal "the plan and the destination disagree". They are
deliberately *distinct* because the user's next action is different
in each case:

* **6 (``COLLISION``)** — a real write was attempted and refused.
  The write did not happen. The user's next step is to pass
  ``--overwrite`` or change the destination, then re-run create.
* **7 (``DRY_RUN_COLLISIONS``)** — a dry-run finished successfully;
  the plan is on stdout (with the collisions listed inside it).
  Nothing was written. The user's next step is to *inspect* the
  plan and then decide.

Collapsing them into one code would force every consumer to inspect
stdout (or a message) to tell the two apart — exactly the
string-matching this contract eliminates. See ADR-0013 (exit codes
as contract) and ADR-0018 (dry-run as pure plan builder).

Note on the safety layer's exit codes
-------------------------------------
The safety layer uses ``FILESYSTEM_CONFLICT`` (3) for structural
refusals (traversal, absolute paths, symlink escapes, unwritable
parents) and ``COLLISION`` (6) for overwrite-able conflicts.

Both are the user's fault and both are recoverable *by editing the
plan*, but only the collision case is also recoverable *by passing
``--overwrite``*. Giving them the same code would force the CLI to
inspect the message to decide whether to offer the flag — the exact
kind of string-matching we are eliminating.

Why these specific codes?
-------------------------
The codes are chosen so that the **first digit** encodes the severity:

- ``0`` — success.
- ``1`` to ``3``, ``6``, ``7`` — the user can fix this without help.
- ``4`` — the project was generated but is not verified.
- ``5`` — the tool is broken. Report it.

``COLLISION`` (6) is a distinct value rather than folding into 3, so
that:

    * CI can tell "the plan is unsafe and needs editing" (3) from
      "the plan needs ``--overwrite``" (6) — two different
      remediations, two different next commands.
    * The CLI's ``--overwrite`` remediation can be offered *only*
      when it can actually help. Offering it for a traversal attempt
      would teach users to pass the flag reflexively, defeating its
      safety purpose.

``DRY_RUN_COLLISIONS`` (7) is a distinct value rather than folding
into 6, so that:

    * CI can tell "a real write was refused" (6) from "a dry-run
      found conflicts" (7) — two different states of the
      filesystem, two different automation patterns.
    * A gating script can say ``if exit == 7: parse plan.json and
      decide`` without mistaking the case for a failed create.

Why the enum lives here, not in ``cli/``
----------------------------------------
The exit code is a **domain decision**, not a CLI decision. Every
``ForgeError`` subclass declares its ``exit_code``; the CLI *uses* the
code. The decision belongs in ``core/``; the use belongs in ``cli/``.
See ``docs/adr/0005-stabilize-exit-codes-as-a-public-contract.md``.

Hierarchy at a glance
---------------------

::

    ForgeError (Exception)              [not slotted; see rationale]
    ├── UserInputError                 (1)
    │   ├── MissingRequiredFlagsError  (1) [slotted dataclass]
    │   └── PromptCancelledError       (1) [slotted dataclass]
    ├── BlueprintError                 (2)
    ├── DiscoveryError                 (2)
    │   └── MalformedBlueprintError    (2) [slotted dataclass]
    ├── FilesystemConflictError        (3)
    ├── VerificationError              (4)
    ├── SafetyError                    (3) [slotted dataclass]
    │   └── CollisionError             (6) [slotted dataclass]
    ├── StructuredError                (5) [slotted dataclass; not exported]
    │   ├── SchemaValidationError      (1) [slotted dataclass]
    │   └── ModelValidationError       (5) [slotted dataclass]
    └── TemplateRenderError            (5) [slotted dataclass]

Note that ``DRY_RUN_COLLISIONS`` (7) is *not* associated with any
exception class. It is used exclusively by the ``create`` command's
``_run_dry_run`` function via a direct ``raise SystemExit(...)`` —
because the dry-run path does not fail; it *succeeds with a
notable outcome*. Representing that outcome as an exception would
be a category error: exceptions are for failures, and the dry-run's
plan is not a failure.

Two design rules follow from this tree:

* **Exit codes are inherited and rarely overridden.** ``DiscoveryError``
  and ``BlueprintError`` both resolve to 2 because "we could not load
  the blueprint you asked for" is the same user-facing problem whether
  the loader rejected it or discovery could not find it. Two exit codes
  for the same recovery would confuse scripts.

* **``CollisionError`` breaks ``SafetyError``'s exit code on purpose.**
  Every safety failure is a user error (3), but a collision is *also*
  recoverable with ``--overwrite`` (6). The subclass override is
  load-bearing: it is what lets CI script "retry with --overwrite" as
  a distinct behaviour rather than a generic "fix the plan" branch.

Mutable structured errors, not frozen dataclasses
-------------------------------------------------
``MissingRequiredFlagsError``, ``PromptCancelledError``,
``MalformedBlueprintError``, ``StructuredError``,
``SchemaValidationError``, ``ModelValidationError``, ``SafetyError``,
and ``CollisionError`` are **plain dataclasses** (``slots=True``
only — no ``frozen=True``). This is deliberate.

Python's exception machinery requires ``__traceback__`` to be a
settable attribute: ``contextlib``, ``unittest``, and every
traceback-preserving library set it after catching. A frozen
dataclass forbids that assignment, so catching a ``frozen`` exception
inside a ``with`` block triggers::

    TypeError: super(type, obj): obj must be an instance or subtype of type

from ``contextlib.__exit__`` when it tries to reattach the traceback.
That is a Python-version-specific landmine and it is *not* a bug in
the calling code — it is the price of combining ``frozen=True`` with
``Exception``.

The same ``TypeError`` also surfaces when a ``@dataclass(slots=True)``
subclass overrides a method that exists on a ``@dataclass(slots=True)``
parent and calls zero-argument ``super()``. The ``slots=True``
decorator rebuilds the class, and the ``__class__`` cell captured by
the method body refers to the *pre-slots* class object — not the one
being instantiated. The failure mode is identical, the trigger is
different, and the fix is the same discipline: do not override
methods that the parent already provides, and if you must, call the
parent explicitly rather than via ``super()``.

The "value object" property that ``frozen=True`` would provide is not
worth that cost. Exceptions are *events*, not values: their identity
is their traceback, not their fields. ``slots=True`` is kept because
it reduces per-instance memory without interfering with exception
machinery.

Why ``ForgeError`` itself is *not* slotted
------------------------------------------
``slots=True`` is applied to the *dataclass* subclasses, not to the
base class. Two reasons:

* ``Exception`` carries state that must remain settable
  (``__traceback__``, ``__context__``, ``__cause__``, ``__notes__``).
  A slotted base would need to declare slots for all of these, which
  is fragile across Python versions.
* The base class is instantiated rarely (only for the
  "unanticipated failure" fallback). The memory saving is negligible;
  the risk of interfering with exception mechanics is not.

The ``hint`` field on dataclass subclasses
------------------------------------------
``hint`` is a **field** with a default on
``MissingRequiredFlagsError``, ``PromptCancelledError``,
``SafetyError``, and ``CollisionError``, not a class attribute. This
is load-bearing: the dataclass-generated ``__init__`` accepts only
declared fields, so a class attribute ``hint = "..."`` would silently
reject the ``hint="..."`` keyword that ``ForgeError.__init__``
documents as public API.

For subclasses that do not need per-instance hint overrides
(``MalformedBlueprintError``, ``StructuredError`` and its concrete
subclasses), ``hint`` stays a class attribute. That is correct: a
caller who wants to override those hints is outside the current
contract, and adding a field would widen the surface without a use
case.

Dataclass inheritance and ``__post_init__``
-------------------------------------------
When both parent and child are ``@dataclass(slots=True)``, the
child's ``__post_init__`` override must call the parent **explicitly**
(``SafetyError.__post_init__(self)``), not via ``super()``. The
zero-argument ``super()`` in a slotted dataclass method fails at
runtime with the ``TypeError`` above, because ``__class__`` was
captured before the ``slots=True`` decoration rebuilt the class.

The simplest, correct shape — and the one this module now uses for
``CollisionError`` — is to **not override ``__post_init__`` at all**
when the parent's implementation is already what the subclass wants.
Dataclasses chain ``__post_init__`` through the MRO at *decoration*
time; a subclass that inherits the parent's method gets it for free,
with no ``super()`` call and no ``TypeError``.

Stability
---------
The following are public API and will not change without a major
version bump:

- The ``ExitCode`` values (``0`` through ``7``).
- The ``ForgeError`` attributes (``exit_code``, ``hint``).
- The subclass names in ``__all__``.
- The constructor signature
  (``message: str, *, hint: str | None = None``) for message-only
  subclasses.
- The dual-mode constructor signatures:
  ``MissingRequiredFlagsError(command, missing, *, hint=None)`` and
  ``PromptCancelledError(prompt=None, *, hint=None)``. ``hint`` is
  keyword-only for both; positional ``hint`` arguments are not part
  of the contract.
- The structured-error constructor signature
  (``source: str, errors: tuple[tuple[str, str], ...]``).
- The safety-error constructor signatures:
  ``SafetyError(findings, operation, *, hint=None)`` and
  ``CollisionError(findings, operation, *, hint=None)``. ``findings``
  is a tuple of ``(code, path, message, hint)`` tuples — the same
  shape that ``forge.safety.errors.Finding`` produces, so the bridge
  is a straight construction.

Adding a new subclass is non-breaking. Removing or renaming one is
breaking. Adding a new attribute to ``ForgeError`` is non-breaking
(as long as it has a default). Removing one is breaking. Adding a
new ``ExitCode`` member is non-breaking (existing codes keep their
meaning); removing or renaming a member is breaking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


# -----------------------------------------------------------------------------
# Exit codes
# -----------------------------------------------------------------------------
class ExitCode(IntEnum):
    """Documented exit codes for the ``forge`` command.

    Each member maps to a distinct failure mode. The codes are
    **stable within a minor version**; changing them requires a major
    version bump.

    ``IntEnum`` is used (rather than ``Enum``) so the values compare
    equal to plain integers. ``sys.exit(ExitCode.SUCCESS)`` works, and
    ``result.exit_code == 0`` works in tests — with a plain ``Enum``,
    both would require an explicit ``.value`` access that is easy to
    forget.
    """

    SUCCESS = 0
    USER_ERROR = 1
    BLUEPRINT_ERROR = 2
    FILESYSTEM_CONFLICT = 3
    VERIFICATION_FAILED = 4
    INTERNAL_ERROR = 5
    # Collisions are recoverable with --overwrite. Distinguished from
    # FILESYSTEM_CONFLICT (3) so CI can offer the flag as a distinct
    # remediation, and so the CLI's hint renderer can say "--overwrite"
    # *only* when it can actually help. See ADR-0017.
    COLLISION = 6
    # A dry-run succeeded but the plan contains collisions. Nothing
    # was written. Distinguished from COLLISION (6) so CI can tell
    # "a real write was refused" from "a dry-run found conflicts"
    # — the two states have different remediations and different
    # automation patterns. See ADR-0018.
    DRY_RUN_COLLISIONS = 7


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
    this failure mode."

    Attributes:
        exit_code: **Class-level.** Subclasses override it
            (e.g., ``exit_code = ExitCode.USER_ERROR``). Read by
            ``forge.cli.errors.handle_exception``.
        hint: **Instance-level.** Set via the constructor. ``None``
            means "no hint." A subclass may set a class-level default
            hint; the constructor's ``hint=None`` does not clobber it.
    """

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR
    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Initialize the error.

        Args:
            message: A one-line description of what went wrong.
            hint: An optional one-line remediation suggestion.

        ``hint`` is keyword-only to prevent the common mistake of
        passing it positionally.
        """
        super().__init__(message)
        if hint is not None:
            self.hint = hint


# -----------------------------------------------------------------------------
# Message-only subclasses
# -----------------------------------------------------------------------------
class UserInputError(ForgeError):
    """Invalid input from the user. Exit code 1."""

    exit_code = ExitCode.USER_ERROR


class BlueprintError(ForgeError):
    """A blueprint is missing, malformed, or unsupported. Exit code 2."""

    exit_code = ExitCode.BLUEPRINT_ERROR


class DiscoveryError(ForgeError):
    """Blueprint discovery could not satisfy a lookup. Exit code 2.

    Inherits ``BlueprintError``'s exit code for the reasons given in
    the module docstring (one recovery for "cannot load this
    blueprint"). Deliberately does *not* define ``__init__`` so the
    dataclass subclass ``MalformedBlueprintError`` can rely on the
    generated initializer.
    """

    exit_code = ExitCode.BLUEPRINT_ERROR


class FilesystemConflictError(ForgeError):
    """A filesystem operation would overwrite or escape the target root.

    Exit code 3 (``FILESYSTEM_CONFLICT``). Used for structural refusals
    that ``--overwrite`` cannot fix: traversal, absolute paths, symlink
    escapes, unwritable parents. **Collisions** — the recoverable case
    — use ``CollisionError`` instead (exit code 6), so the CLI's
    remediation hint can be precise.
    """

    exit_code = ExitCode.FILESYSTEM_CONFLICT


class VerificationError(ForgeError):
    """One or more verification checks failed. Exit code 4."""

    exit_code = ExitCode.VERIFICATION_FAILED


# -----------------------------------------------------------------------------
# Safety bridge subclasses
# -----------------------------------------------------------------------------
# These bridge the *implementation* safety errors produced by
# ``forge.safety`` (which are shaped as ``SafetyError(findings,
# operation)`` with a rich ``Finding`` payload) to the CLI's error
# hierarchy (which is shaped as ``ForgeError(message, hint=...)``).
#
# The bridge exists so:
#
#   * ``forge.safety`` stays free of any dependency on ``forge.cli``
#     or ``forge.core`` (it is a primitive; primitives do not depend
#     on their consumers),
#   * the CLI handler can dispatch on ``isinstance(exc, ForgeError)``
#     without string matching, and
#   * the exit codes — the user-facing contract — are decided in
#     ``core`` where ADR-0005 says they belong.
#
# A caller (the CLI's ``create`` command, or any future caller of
# ``forge.safety``) catches the safety-layer's ``SafetyError`` /
# ``CollisionError`` and re-raises the corresponding type from here,
# carrying the findings through unchanged. One-line translation, one
# place where the exit code lives.
#
# Why two classes, not one with a bool:
#   * ``SafetyError`` maps to FILESYSTEM_CONFLICT (3): "fix the plan,
#     no flag will help." The CLI hint says exactly that.
#   * ``CollisionError`` maps to COLLISION (6): "you can retry with
#     --overwrite, or edit the plan." The CLI hint says *both*, in
#     that order, because --overwrite is the shortest path but not
#     always the right one.
# Conflating them would force every caller (and the CLI hint
# renderer, and CI scripts) to inspect a message to distinguish the
# two remediations.
#
# ``findings`` shape
# ------------------
# ``tuple[tuple[str, str, str, str], ...]`` — one tuple per violation
# in the order ``(code, path, message, hint)``. This is a *lossy*
# projection of ``forge.safety.errors.Finding`` (which also carries
# ``root``), on purpose:
#
#   * ``root`` is included in the ``message`` at translation time
#     (the safety layer's own ``format()`` already interleaves it),
#     so the CLI does not need a separate field to render it.
#   * The four-field shape is the *contract* for these errors; a
#     future addition (say, a ``severity``) would be an additive
#     change to the tuple, with a documented schema bump.
#   * Using a tuple of primitives (not the original ``Finding``
#     dataclasses) keeps ``core.exceptions`` free of any dependency
#     on ``forge.safety`` — the dependency direction stays
#     ``safety → core-exceptions``, never the reverse.
@dataclass(slots=True)
class SafetyError(ForgeError):
    """A safety-layer refusal that ``--overwrite`` cannot resolve.

    **Exit code:** ``FILESYSTEM_CONFLICT`` (3).

    **When raised:** A path escapes the root, is absolute, contains a
    symlink, is too long, contains a NUL byte, or names an unwritable
    parent. The safety layer rejects *all* of these at plan time, and
    the CLI translates the rejection into this type so the handler
    can render it with a fix-the-plan hint.

    **What the user should do:** Edit the plan (rename a destination,
    remove a traversal, fix a symlink). ``--overwrite`` cannot help
    because the problem is *where* the plan writes, not *whether* it
    replaces an existing file.

    Attributes:
        findings: One ``(code, path, message, hint)`` tuple per
            violation. All four are non-empty (``path`` may be ``""``
            for path-less findings like "root is not writable").
        operation: The operation label (e.g. ``"create"``,
            ``"commit"``). Included so a log line names what was being
            attempted, not just what failed.
        hint: A *generic* remediation ("fix the plan") that overrides
            the per-finding hints for the top-level message. Per-
            finding hints are still rendered (see
            ``forge.cli.errors.print_safety_findings``); this field
            covers the "what do I do overall" case.
    """

    findings: tuple[tuple[str, str, str, str], ...] = ()
    operation: str = "<unknown>"
    hint: str | None = field(
        default=(
            "The plan writes outside the destination or into an unsafe "
            "location. Edit the blueprint's file entries or choose a "
            "different destination. --overwrite cannot help here."
        )
    )

    # Class-level so subclasses can override without re-declaring.
    # A field here would let a caller pass ``exit_code=0`` and bypass
    # the contract — same reasoning as ``StructuredError``.
    exit_code = ExitCode.FILESYSTEM_CONFLICT

    def __post_init__(self) -> None:
        """Compose the message and initialize ``Exception.args``.

        The message lists every finding on its own line, prefixed by
        the finding's code. That is what makes ``str(exc)`` useful for
        both humans (each line names the code and the fix) and log
        scrapers (each line begins with a stable ``[code]`` marker).
        """
        if not self.findings:
            message = f"Filesystem safety refused the plan during {self.operation}."
        else:
            header = (
                f"Filesystem safety refused the plan during "
                f"{self.operation} ({len(self.findings)} issue(s)):"
            )
            lines = [header]
            for code, path, message, _hint in self.findings:
                rendered_path = path or "<root>"
                lines.append(f"  • [{code}] {rendered_path}: {message}")
            message = "\n".join(lines)
        Exception.__init__(self, message)


@dataclass(slots=True)
class CollisionError(SafetyError):
    """A plan would overwrite existing files, and ``--overwrite`` was not passed.

    **Exit code:** ``COLLISION`` (6). Distinct from ``SafetyError``'s
    3 because the remediation is different: pass ``--overwrite``
    (which creates backups), or edit the plan. The CLI hint says both,
    in that order.

    **When raised:** The safety layer's ``detect_collisions`` found
    destinations that exist on disk and ``--overwrite`` was not
    consented. Also raised for plan-internal case-insensitive
    duplicates — those are *not* suppressed by ``--overwrite`` (the
    flag consents to replacing disk files, not to a plan that behaves
    differently on different filesystems), so they surface here with
    a distinct code in the findings.

    **What the user should do:** Re-run with ``--overwrite`` to
    replace the existing files (a timestamped backup is created for
    each), or edit the plan so it does not target those paths.

    The ``hint`` default names both options. A caller who knows the
    specific remediation (e.g. the CLI, which knows whether
    ``--overwrite`` is even a valid flag for this command) can override
    the hint with context-specific wording.

    Why no ``__post_init__`` override
    ---------------------------------
    An earlier revision of this class overrode ``__post_init__`` and
    called ``super().__post_init__()`` to "customise the header".
    That combination does not work in the presence of
    ``@dataclass(slots=True)`` on both parent and child: the
    ``slots=True`` decorator rebuilds the child class, and the
    zero-argument ``super()`` inside the overridden method resolves
    against a stale ``__class__`` cell — producing ``TypeError:
    super(type, obj): obj must be an instance or subtype of type``
    the moment a ``CollisionError`` is constructed.

    The override was also *doing nothing useful*. The message
    composition in ``SafetyError.__post_init__`` is identical to what
    a "custom header" version would have produced — the *collision-
    specific* text belongs in the ``hint`` field (which the CLI
    renders after the message), not in the message itself.

    The correct shape — used here — is to *inherit* ``__post_init__``
    rather than override it. Dataclasses chain ``__post_init__``
    through the MRO at *decoration* time, so the parent's method runs
    automatically, message composition is identical, and there is no
    ``super()`` call to trigger the ``slots=True`` landmine.
    """

    hint: str | None = field(
        default=(
            "Re-run with --overwrite to replace the existing files "
            "(a timestamped backup will be created), or edit the plan "
            "so it does not target those paths."
        )
    )

    exit_code = ExitCode.COLLISION


# -----------------------------------------------------------------------------
# Dual-mode input handling subclasses
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class MissingRequiredFlagsError(UserInputError):
    """Non-interactive mode: required flags were not supplied. Exit code 1.

    Attributes:
        command: The command name (e.g., ``"create"``).
        missing: The missing flag names, without the ``--`` prefix,
            in stable declaration order. Normalized to a deduplicated
            tuple in ``__post_init__``.
        hint: Defaults to the class-level value; raisers with better
            context may override it.
    """

    command: str
    missing: tuple[str, ...]
    hint: str | None = field(
        default="Pass the missing flags, or re-run in an interactive terminal."
    )

    def __post_init__(self) -> None:
        self.missing = tuple(dict.fromkeys(self.missing))
        formatted = ", ".join(f"--{flag}" for flag in self.missing)
        message = f"Missing required option(s) for non-interactive mode: {formatted}."
        Exception.__init__(self, message)


@dataclass(slots=True)
class PromptCancelledError(UserInputError):
    """Interactive mode: the user aborted a prompt. Exit code 1.

    Attributes:
        prompt: The interrupted prompt message, if known.
        hint: Defaults to the class-level value; raisers with better
            context may override it.
    """

    prompt: str | None = None
    hint: str | None = field(
        default=(
            "Re-run when ready, or use --no-interactive with explicit "
            "flags for scriptable usage."
        )
    )

    def __post_init__(self) -> None:
        if self.prompt:
            message = f"Interactive prompt cancelled by user at '{self.prompt}'."
        else:
            message = "Interactive prompt cancelled by user."
        Exception.__init__(self, message)


# -----------------------------------------------------------------------------
# Discovery-specific structured subclass
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class MalformedBlueprintError(DiscoveryError):
    """A candidate blueprint could not be parsed or validated. Exit code 2.

    Attributes:
        source_label: The candidate's provenance label.
        reason: The underlying failure, in human-readable form.
    """

    source_label: str
    reason: str

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.source_label}: {self.reason}")


# -----------------------------------------------------------------------------
# Structured errors
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class StructuredError(ForgeError):
    """Base for errors carrying RFC 6901 JSON-Pointer-tagged violations.

    Not part of the public surface. Subclass to give a specific
    failure mode its own exit code and message while keeping the
    ``.errors`` shape uniform.
    """

    exit_code = ExitCode.INTERNAL_ERROR

    source: str
    errors: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        Exception.__init__(self, self.__str__())

    def __str__(self) -> str:
        if not self.errors:
            return f"Blueprint '{self.source}' failed validation."
        lines = [f"Blueprint '{self.source}' failed validation:"]
        for pointer, message in self.errors:
            rendered_pointer = pointer if pointer else "/"
            lines.append(f"  • {rendered_pointer}: {message}")
        return "\n".join(lines)


@dataclass(slots=True)
class SchemaValidationError(StructuredError):
    """A blueprint document did not conform to the bundled JSON Schema.

    Exit code 1 (user error). Hint points at SCHEMA_REFERENCE.md.
    """

    exit_code = ExitCode.USER_ERROR
    hint = "See docs/reference/SCHEMA_REFERENCE.md for field-level guidance."

    def __str__(self) -> str:
        if not self.errors:
            return f"Blueprint '{self.source}' failed schema validation."
        lines = [f"Blueprint '{self.source}' failed schema validation:"]
        for pointer, message in self.errors:
            rendered_pointer = pointer if pointer else "/"
            lines.append(f"  • {rendered_pointer}: {message}")
        return "\n".join(lines)


@dataclass(slots=True)
class ModelValidationError(StructuredError):
    """A schema-valid blueprint failed Pydantic validation.

    Exit code 5 (internal error). Hint names the parity checks that
    guard against this (``make models-check`` + the Hypothesis parity
    test), so the diff is obvious to whoever reports it.
    """

    exit_code = ExitCode.INTERNAL_ERROR
    hint = (
        "This is a Forge bug, not an input error. Run "
        "`make models-check` and `pytest tests/unit/models/test_schema_model_parity.py`, "
        "then report the diff."
    )

    def __str__(self) -> str:
        if not self.errors:
            return (
                f"Blueprint '{self.source}' passed schema validation but "
                f"failed model validation — this is a Forge bug."
            )
        lines = [
            f"Blueprint '{self.source}' passed schema validation but "
            f"failed model validation — this is a Forge bug:"
        ]
        for pointer, message in self.errors:
            rendered_pointer = pointer if pointer else "/"
            lines.append(f"  • {rendered_pointer}: {message}")
        return "\n".join(lines)


@dataclass(slots=True)
class TemplateRenderError(StructuredError):
    """A template failed during rendering. Exit code 5.

    The public rendering-side type is
    ``forge.rendering.errors.TemplateRenderError``; this subclass
    exists so the CLI handler can dispatch on ``isinstance(exc,
    ForgeError)`` without string matching.
    """

    exit_code = ExitCode.INTERNAL_ERROR
    hint = (
        "This is a Forge bug, not an input error. Report it with the "
        "template path and the offending line; include the "
        "`forge --version` output."
    )

    def __str__(self) -> str:
        if not self.errors:
            return f"Template '{self.source}' failed to render — this is a Forge bug."
        lines = [f"Template '{self.source}' failed to render — this is a Forge bug:"]
        for pointer, message in self.errors:
            rendered_pointer = pointer if pointer else "/"
            lines.append(f"  • {rendered_pointer}: {message}")
        return "\n".join(lines)


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
# Adding to ``__all__`` is non-breaking; removing from it is breaking.
# ``StructuredError`` (the base) is not exported, because callers
# should catch the actionable subtype. The safety bridge exports both
# concrete types because callers legitimately branch on
# ``isinstance(exc, CollisionError)`` to decide whether to offer
# ``--overwrite``.
__all__ = [
    "BlueprintError",
    "CollisionError",
    "DiscoveryError",
    "ExitCode",
    "FilesystemConflictError",
    "ForgeError",
    "MalformedBlueprintError",
    "MissingRequiredFlagsError",
    "ModelValidationError",
    "PromptCancelledError",
    "SafetyError",
    "SchemaValidationError",
    "TemplateRenderError",
    "UserInputError",
    "VerificationError",
]
