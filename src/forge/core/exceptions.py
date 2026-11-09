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
change and requires a major version bump.

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
- ``1`` to ``3`` — the user can fix this without help.
- ``4`` — the project was generated but is not verified.
- ``5`` — the tool is broken. Report it.

The ``hint`` attribute
----------------------
Every ``ForgeError`` can carry an optional ``hint`` — a one-line
remediation suggestion. The handler renders the hint on a separate,
dimmed line, so the user sees the *problem* and the *fix* together.

``hint`` has two legitimate sources:

* **Class-level default.** Set on the subclass
  (``SchemaValidationError.hint = "See ..."``). Applies when the
  caller does not override it. Used for stable, type-wide
  remediation text.
* **Instance-level override.** Passed as a keyword to the
  constructor. Subclasses that declare ``hint`` as a dataclass field
  with a default accept this; classes that keep it a class attribute
  do not expose the keyword. See the field-vs-attribute rationale
  below.

The ``SchemaValidationError`` and ``ModelValidationError`` classes
declare a class-level hint *and* inherit a constructor that accepts
an override via the base ``ForgeError.__init__`` path — but as
dataclasses they call ``Exception.__init__`` directly and therefore
do **not** accept the ``hint=`` keyword. The class-level hint is
fixed for these two types, by design: their hints do not need
per-instance context, and a keyword would widen the surface for no
use case. If a future need emerges, adding ``hint`` as a field is
non-breaking.

Why the enum lives here, not in ``cli/``
----------------------------------------
The exit code is a **domain decision**, not a CLI decision. Every
``ForgeError`` subclass declares its ``exit_code``; the CLI *uses*
the code. The decision belongs in ``core/``; the use belongs in
``cli/``. See ``docs/adr/0005-stabilize-exit-codes-as-a-public-contract.md``.

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
    └── StructuredError                (5) [slotted dataclass]
        ├── SchemaValidationError      (1) [slotted dataclass]
        └── ModelValidationError       (5) [slotted dataclass]

Two design rules follow from this tree:

* **Exit codes are inherited and rarely overridden.** ``DiscoveryError``
  and ``BlueprintError`` both resolve to 2 because "we could not load
  the blueprint you asked for" is the same user-facing problem whether
  the loader rejected it or discovery could not find it. Two exit codes
  for the same recovery would confuse scripts.

* **``StructuredError`` breaks the base's exit code on purpose.** Its
  subclasses carry *structured* violations rather than a message; the
  exit code depends on whether the violation is the user's fault
  (schema → 1) or Forge's fault (model → 5). The subclass override is
  load-bearing, not decorative.

Mutable structured errors, not frozen dataclasses
-------------------------------------------------
``MissingRequiredFlagsError``, ``PromptCancelledError``,
``MalformedBlueprintError``, ``StructuredError``,
``SchemaValidationError``, and ``ModelValidationError`` are **plain
dataclasses** (``slots=True`` only — no ``frozen=True``). This is
deliberate.

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
  (``__traceback__``, ``__context__``, ``__cause__``,
  ``__notes__``). A slotted base would need to declare slots for all
  of these, which is fragile across Python versions.
* The base class is instantiated rarely (only for the
  "unanticipated failure" fallback). The memory saving is
  negligible; the risk of interfering with exception mechanics is
  not.

The ``hint`` field on dataclass subclasses
------------------------------------------
``hint`` is a **field** with a default on
``MissingRequiredFlagsError`` and ``PromptCancelledError``, not a
class attribute. This is load-bearing: the dataclass-generated
``__init__`` accepts only declared fields, so a class attribute
``hint = "..."`` would silently reject the ``hint="..."`` keyword
that ``ForgeError.__init__`` documents as public API. Declaring it
as a field with a default:

* Accepts the keyword override (public API contract preserved).
* Preserves the class-level default behaviour (a caller who omits
  ``hint`` still gets the remediation line).
* Keeps the dataclass ``__init__`` signature self-documenting.

For subclasses that do not need per-instance hint overrides
(``MalformedBlueprintError``, ``StructuredError`` and its concrete
subclasses), ``hint`` stays a class attribute. That is correct: a
caller who wants to override those hints is outside the current
contract, and adding a field would widen the surface without a
use case.

Stability
---------
The following are public API and will not change without a major
version bump:

- The ``ExitCode`` values (``0`` through ``5``).
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
- The ``.errors`` attribute shape on structured errors
  (``tuple[tuple[str, str], ...]``).

Adding a new subclass is non-breaking. Removing or renaming one is
breaking. Adding a new attribute to ``ForgeError`` is non-breaking
(as long as it has a default). Removing one is breaking.
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

    # Not slotted: ``Exception`` requires settable traceback/context
    # attributes (see module docstring, "Why ForgeError itself is not
    # slotted"). Subclasses that need memory savings are dataclasses
    # with ``slots=True``.
    exit_code: ExitCode = ExitCode.INTERNAL_ERROR
    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Initialize the error.

        Args:
            message: A one-line description of what went wrong.
                Rendered as the first line of the error output.
                Should be imperative and actionable
                ("project name is required"), not vague
                ("an error occurred").
            hint: An optional one-line remediation suggestion. Should
                tell the user *what to do next* ("pass --name
                my-project"), not what they did wrong (the message
                already says that).

        ``hint`` is keyword-only to prevent the common mistake of
        passing it positionally.
        """
        super().__init__(message)
        # Only assign if the caller actually passed a hint. This keeps
        # a class-level default hint in place when the caller does not
        # pass one — a subclass that sets a default would otherwise be
        # clobbered by ``None``.
        if hint is not None:
            self.hint = hint


# -----------------------------------------------------------------------------
# Message-only subclasses
# -----------------------------------------------------------------------------
class UserInputError(ForgeError):
    """Invalid input from the user.

    **Exit code:** ``ExitCode.USER_ERROR`` (1).

    **When raised:** A missing required flag, an invalid value, a
    "user said no" prompt rejection.

    **What the user should do:** Re-run the command with valid input.
    The message and hint say what is wrong and how to fix it.
    """

    exit_code = ExitCode.USER_ERROR


class BlueprintError(ForgeError):
    """A blueprint is missing, malformed, or unsupported.

    **Exit code:** ``ExitCode.BLUEPRINT_ERROR`` (2).

    **When raised:** The requested blueprint ID is not in the
    catalogue, the manifest does not validate against the schema, or
    the blueprint's ``min_forge_version`` is newer than the installed
    Forge.

    **What the user should do:** Choose a different blueprint, upgrade
    Forge, or fix the blueprint. The hint tells them which.
    """

    exit_code = ExitCode.BLUEPRINT_ERROR


class DiscoveryError(ForgeError):
    """Blueprint discovery could not satisfy a lookup.

    **Exit code:** ``ExitCode.BLUEPRINT_ERROR`` (2), inherited. Two
    exit codes for "the blueprint you asked for cannot be loaded"
    would confuse scripts: whether the loader rejected the manifest
    or discovery could not find it, the recovery is the same (choose
    a different blueprint, or fix the local one).

    **When raised:** ``BlueprintDiscoveryService.load(id)`` when no
    source yields a blueprint with the requested id.

    **What the user should do:** Run ``forge list`` to see available
    blueprints, or fix the local override that shadowed the requested
    id.

    .. note::
       This class deliberately does not define ``__init__``. Dataclass
       subclasses (``MalformedBlueprintError``) rely on the base
       having no custom initializer; adding one here would silently
       bypass the dataclass-generated ``__init__`` on those
       subclasses. If a custom initializer becomes necessary, audit
       every dataclass subclass and either drop ``slots=True`` or
       provide an explicit ``__post_init__`` forwarding path.
    """

    # Deliberately redeclared rather than inherited from a nonexistent
    # intermediate: the parent is ``ForgeError`` (which defaults to
    # INTERNAL_ERROR), so we must be explicit here — silently
    # inheriting INTERNAL_ERROR would be a real bug.
    exit_code = ExitCode.BLUEPRINT_ERROR


class FilesystemConflictError(ForgeError):
    """A filesystem operation would overwrite or escape the target root.

    **Exit code:** ``ExitCode.FILESYSTEM_CONFLICT`` (3).

    **When raised:** The target directory already exists and
    ``--overwrite`` was not passed; a template path escapes the
    staging directory; a permission error prevents writing.

    **What the user should do:** Choose a different ``--output``,
    pass ``--overwrite`` (if certain), or fix permissions.
    """

    exit_code = ExitCode.FILESYSTEM_CONFLICT


class VerificationError(ForgeError):
    """One or more verification checks failed.

    **Exit code:** ``ExitCode.VERIFICATION_FAILED`` (4).

    **When raised:** A required file is missing, the test suite
    fails, the linter reports errors, the Docker build fails.

    **What the user should do:** Address the failed checks. The hint
    tells them which check failed and how to inspect the diagnostic.
    """

    exit_code = ExitCode.VERIFICATION_FAILED


# -----------------------------------------------------------------------------
# Dual-mode input handling subclasses
# -----------------------------------------------------------------------------
# These subclasses exist specifically to give the interactive and
# non-interactive paths of the ``create`` command distinct, testable
# failure types. Both map to ``ExitCode.USER_ERROR`` (1) because the
# user can resolve both without help — one by passing flags, the other
# by re-running the wizard.
#
# Keeping them as separate types (rather than raising bare
# ``UserInputError``) is not cosmetic: the CLI error handler can render
# each with a mode-appropriate hint, and tests can assert on the
# specific failure path rather than string-matching a message. That is
# the difference between a contract and a suggestion.
#
# Field ordering note
# -------------------
# Both classes declare ``hint`` as a dataclass field with a default.
# Dataclass requires fields with defaults to appear *after* fields
# without, so ``hint`` is always last. This ordering also reads
# naturally: the primary arguments first, the optional override last.
# The keyword-only marker in the docstrings reflects how callers
# should use it, even though dataclasses do not enforce keyword-only
# by default. Callers who pass it positionally get a working call —
# that is a deliberate concession to dataclass mechanics, not a
# contract decision; the documented signature uses ``*``.
@dataclass(slots=True)
class MissingRequiredFlagsError(UserInputError):
    """Non-interactive mode: required flags were not supplied.

    **Exit code:** ``ExitCode.USER_ERROR`` (1), inherited.

    **When raised:** ``forge create`` is run without a TTY (stdin is
    not a terminal) and one or more required flags are absent.

    **What the user should do:** Pass the missing flags, or re-run in
    an interactive terminal. The message lists *every* missing flag
    so the caller fixes everything in a single edit.

    Attributes:
        command: The command name (e.g., ``"create"``).
        missing: The missing flag names, without the ``--`` prefix,
            in stable declaration order. Normalized to a deduplicated
            tuple in ``__post_init__``; a caller that passed
            ``["name", "name"]`` sees one rendered flag, matching the
            "fix everything in a single edit" contract.
        hint: A one-line remediation suggestion. Defaults to the
            class-level value ("Pass the missing flags, or re-run in
            an interactive terminal."). Raisers with better context
            may override it.
    """

    command: str
    missing: tuple[str, ...]
    hint: str | None = field(
        default="Pass the missing flags, or re-run in an interactive terminal."
    )

    def __post_init__(self) -> None:
        """Normalize ``missing`` and initialize ``Exception.args``.

        Two responsibilities, both load-bearing:

        1. **Normalize ``missing`` to a deduplicated tuple.** The
           public type is ``tuple[str, ...]``, but callers habitually
           pass a list comprehension or a generator, and may pass the
           same flag twice (e.g. from a merged source list). We
           deduplicate while preserving order:
           ``(a, b, a)`` becomes ``(a, b)``. A generator passed
           directly would be consumed once, which is a subtle footgun
           the dataclass cannot express in its type.

        2. **Compose the message and feed it to ``Exception.args``.**
           Dataclasses generate ``__init__`` for us, so the base
           ``Exception.__init__`` is never called on the normal path.
           Without this, ``str(exc)`` would return an empty string and
           pickle round-trips would lose the message.

        The message is built *after* normalization so it reflects the
        canonical tuple, not the caller's input.
        """
        # ``dict.fromkeys`` preserves insertion order and deduplicates.
        # Cheaper and more readable than a manual seen-set loop, and
        # guaranteed O(n) by CPython's dict implementation.
        self.missing = tuple(dict.fromkeys(self.missing))

        formatted = ", ".join(f"--{flag}" for flag in self.missing)
        message = f"Missing required option(s) for non-interactive mode: " f"{formatted}."
        Exception.__init__(self, message)


@dataclass(slots=True)
class PromptCancelledError(UserInputError):
    """Interactive mode: the user aborted a prompt.

    **Exit code:** ``ExitCode.USER_ERROR`` (1), inherited.

    **When raised:** The user pressed Ctrl+C or sent EOF at a
    questionary prompt. This is a *clean* cancellation: the process
    should exit quietly with a short message and no traceback.

    **What the user should do:** Re-run the command when ready. The
    hint reminds them that ``--no-interactive`` plus flags is
    available if they prefer.

    Attributes:
        prompt: The prompt message that was interrupted, if known.
            Included in the message so the user can see *where* they
            cancelled. ``None`` **and** an empty string both mean
            "cannot identify the step" — the truthiness check in
            ``__post_init__`` treats them identically, and this is
            deliberate: a caller who forgot to pass ``prompt`` gets
            the same generic message as one who passed ``""``.
        hint: A one-line remediation suggestion. Defaults to the
            class-level value ("Re-run when ready, or use
            --no-interactive ..."). Raisers with better context may
            override it.
    """

    prompt: str | None = None
    hint: str | None = field(
        default=(
            "Re-run when ready, or use --no-interactive with explicit "
            "flags for scriptable usage."
        )
    )

    def __post_init__(self) -> None:
        """Compose the message and initialize ``Exception.args``.

        The message varies based on whether ``prompt`` is truthy:
        with a prompt, it names the step; without (``None`` or
        ``""``), it stays generic. Both forms end in "cancelled by
        user" so log scrapers and tests keying on that phrase work
        uniformly.
        """
        if self.prompt:
            message = f"Interactive prompt cancelled by user at " f"'{self.prompt}'."
        else:
            message = "Interactive prompt cancelled by user."
        Exception.__init__(self, message)


# -----------------------------------------------------------------------------
# Discovery-specific structured subclass
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class MalformedBlueprintError(DiscoveryError):
    """A candidate blueprint could not be parsed or validated.

    **Exit code:** ``ExitCode.BLUEPRINT_ERROR`` (2), inherited from
    ``DiscoveryError``.

    **When raised:** ``forge.discovery.resolver._parse`` when a
    candidate file is unreadable, is invalid YAML, is not a mapping at
    the document root, fails schema validation, or fails model
    validation. The resolver catches this error and skips the
    candidate with a warning; it is almost never seen by end users
    directly — that is, until *every* candidate for a requested id is
    malformed, at which point the aggregate surfaces as
    ``DiscoveryError`` from ``load()``.

    **What the user should do:** Fix the named file. The ``reason``
    string carries the underlying failure so the message is
    self-contained; no separate logging lookup is needed.

    .. note::
       This dataclass relies on ``DiscoveryError`` having no
       ``__init__`` of its own. Adding one to the parent would
       require dropping ``slots=True`` here or forwarding explicitly
       through ``__post_init__``; see the parent class's note.

    Attributes:
        source_label: The candidate's provenance label, in the
            resolver's format (e.g.,
            ``"local:/home/u/.forge/blueprints/x/blueprint.yaml"``).
            Lets the user find the offending file directly.
        reason: The underlying failure, in human-readable form. May
            embed a nested exception's ``__str__``; that is
            intentional, because the wrap preserves the causal chain
            while keeping the field greppable.
    """

    source_label: str
    reason: str

    def __post_init__(self) -> None:
        """Compose ``<label>: <reason>`` and set ``Exception.args``.

        The format is stable and parseable: tests assert on the label
        prefix to verify *which* candidate was skipped, and on the
        reason substring to verify *why*. Keeping the two components
        as separate fields means the assertion does not depend on the
        message template, which may change independently.
        """
        Exception.__init__(self, f"{self.source_label}: {self.reason}")


# -----------------------------------------------------------------------------
# Structured errors (schema and model validation of blueprint documents)
# -----------------------------------------------------------------------------
# Unlike every other subclass in this module, the two errors below are
# not message-only: they carry a tuple of ``(json_pointer, message)``
# pairs so callers can *act* on violations without parsing prose. They
# share a base, ``StructuredError``, so the ``.errors`` accessor
# contract is documented once and the CLI handler can render both
# uniformly.
#
# Why the base is *not* exported in ``__all__``: the two concrete
# subclasses are the vocabulary. Callers should catch the specific one
# they can act on (schema → fix the blueprint; model → report a bug).
# ``StructuredError`` exists for the handler to share rendering code.
@dataclass(slots=True)
class StructuredError(ForgeError):
    """Base for errors carrying RFC 6901 JSON-Pointer-tagged violations.

    Not part of the public surface. Subclass to give a specific
    failure mode its own exit code and message while keeping the
    ``.errors`` shape uniform.

    Deliberately *not* frozen: Python's exception machinery assigns to
    ``__traceback__`` after catching, and a frozen dataclass forbids
    that assignment (see module docstring).

    Attributes:
        source: A caller-supplied label (usually a file path) used to
            make the message self-contained. Never inspected.
        errors: A tuple of ``(json_pointer, message)`` pairs, sorted
            by pointer for deterministic output. Empty pointers
            (``""``) refer to the document root. Pointers follow
            RFC 6901.
    """

    # Exit code is class-level, not a dataclass field, because it is a
    # *contract* (subclasses override to give meaning), not a
    # per-instance parameter. This distinction is load-bearing: if it
    # were a field, callers could pass ``exit_code=0`` and bypass the
    # contract silently.
    exit_code = ExitCode.INTERNAL_ERROR

    source: str
    errors: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Initialize ``Exception.args`` so ``str()`` and pickling work.

        ``Exception.__init__`` is normally called in ``__init__``, but
        a dataclass generates ``__init__`` for us. We call the base
        initializer here with the message that ``__str__`` will
        render, so ``repr(exc)`` and pickle round-trips behave as
        callers expect from an exception.

        Note: this eagerly formats the message, which means
        ``__str__`` runs once at construction and again whenever the
        caller formats the exception. For the error counts in
        practice (single digits) that is a non-issue; for very large
        batches the extra pass is negligible. If profiling ever shows
        this matters, switch to storing the raw fields and formatting
        lazily — but keep ``Exception.args`` populated so pickling
        continues to work.
        """
        Exception.__init__(self, self.__str__())

    def __str__(self) -> str:
        """Render one bullet per violation, with JSON Pointer paths.

        Two audiences are served by the same string:

        * Humans see ``• /variables/0/type: ...``.
        * Log scrapers keyed on the leading ``•`` can split on it.

        Scripts should use ``errors`` directly, not parse this string.
        """
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

    **Exit code:** ``ExitCode.USER_ERROR`` (1). A blueprint the user
    authored and shipped invalidly is a user error. A broken *bundled*
    schema is a packaging bug caught earlier, at validator
    construction — the two failure modes are distinguished by
    construction, not by string matching.

    **When raised:**
    ``forge.verification.validate_blueprint_document`` when the
    parsed document does not conform to the schema.

    **What the user should do:** Fix the reported violations. The
    ``errors`` tuple names every offending JSON Pointer; the hint
    points at ``SCHEMA_REFERENCE.md`` for field-level guidance.
    """

    # Override the base's exit code and hint. Both are class-level
    # because they are contract, not state. Overriding via a field
    # here would widen the constructor signature without a current
    # use case; the class-attribute form is intentional. See the
    # module docstring, "The hint field on dataclass subclasses."
    exit_code = ExitCode.USER_ERROR
    hint = "See docs/reference/SCHEMA_REFERENCE.md for field-level guidance."

    def __str__(self) -> str:
        """Render with a "schema validation" prefix.

        The base ``StructuredError.__str__`` says "failed validation",
        which is correct for both subclasses but does not tell the
        user *which* validator fired. This override names the schema
        explicitly, because the two failure modes have different
        remedies (fix your blueprint vs. report a Forge bug).
        """
        if not self.errors:
            return f"Blueprint '{self.source}' failed schema validation."
        lines = [f"Blueprint '{self.source}' failed schema validation:"]
        for pointer, message in self.errors:
            rendered_pointer = pointer if pointer else "/"
            lines.append(f"  • {rendered_pointer}: {message}")
        return "\n".join(lines)


@dataclass(slots=True)
class ModelValidationError(StructuredError):
    """A schema-valid blueprint document failed Pydantic validation.

    **Exit code:** ``ExitCode.INTERNAL_ERROR`` (5). A document that
    satisfies the canonical schema but is rejected by the generated
    Pydantic models is a **Forge bug**, not a user mistake. The two
    engines are required to agree (ADR-0008), enforced by a Hypothesis
    parity test and by ``make models-check``. If this error is
    raised, that enforcement has failed somewhere.

    **When raised:**
    ``forge.verification.validate_blueprint_document`` after schema
    validation succeeds but ``Blueprint.model_validate`` fails.

    **What the user should do:** Report it. The message names the
    parity regression, and the hint points at the two checks that
    guard against it.
    """

    exit_code = ExitCode.INTERNAL_ERROR
    hint = (
        "This is a Forge bug, not an input error. Run "
        "`make models-check` and `pytest tests/unit/models/test_schema_model_parity.py`, "
        "then report the diff."
    )

    def __str__(self) -> str:
        """Render with a "model validation" prefix and a bug-report footer.

        The footer is what distinguishes this from a schema error at a
        glance in CI logs: schema errors end with a bullet list (fix
        the blueprint); model errors end with "this is a Forge bug"
        (report it). The distinction matters because the two have
        opposite remedies.
        """
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


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
# Adding to ``__all__`` is non-breaking; removing from it is breaking.
#
# ``StructuredError`` is deliberately *not* in ``__all__``. It is a
# documented common ancestor, not a catch target: callers should catch
# ``SchemaValidationError`` (actionable: fix the blueprint) or
# ``ModelValidationError`` (actionable: report a bug) — never the base,
# because the correct response differs. The class remains importable
# (``from forge.core.exceptions import StructuredError``) for handlers
# that legitimately need to share rendering code across both subtypes.
__all__ = [
    "BlueprintError",
    "DiscoveryError",
    "ExitCode",
    "FilesystemConflictError",
    "ForgeError",
    "MalformedBlueprintError",
    "MissingRequiredFlagsError",
    "ModelValidationError",
    "PromptCancelledError",
    "SchemaValidationError",
    "UserInputError",
    "VerificationError",
]
