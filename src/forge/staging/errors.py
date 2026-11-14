"""Staging-phase errors — one shape, one audience, one exit code.

Purpose
-------
Every failure produced by the staging engine (a missing template
variable, an unreadable source file, a structural mismatch between the
plan and the staged tree) is represented as a `RenderingError` carrying
a tuple of `Finding` values. Nothing in this module raises on
construction; callers decide whether a given collection of findings is
fatal, reportable, or ignorable.

Design rules
------------
1. **Findings are data, not stack traces.** Every `Finding` contains
   exactly the four things a user needs to act: where (`path`), what
   (`message`), why it matters / how to fix (`hint`), and a stable
   machine-readable classifier (`code`). No `__cause__`, no frames, no
   file paths from the tool's own source tree.

2. **Aggregated, not first-only.** `RenderingError` holds *all*
   findings from a run. A user with ten broken templates fixes ten
   templates in one pass, not ten. The engine collects; this type
   reports.

3. **Same shape as every other structured error in the codebase.**
   `loading`, `planning`, `conditions`, `safety`, and `dryrun` all use
   the same `Finding` concept (`code`, `path`, `message`, `hint`). The
   reason is not aesthetic consistency — it is that a single CLI
   renderer, a single JSON contract, and a single exit-code mapping can
   service every phase if they all speak one dialect. Introducing a
   parallel vocabulary here would force each consumer to learn a second
   one.

4. **The exit code is not this module's concern.** `RenderingError` is a
   domain type; mapping it to a process exit status belongs to the CLI
   boundary (`ExitCode.RENDERING_ERROR = 8`). Keeping that mapping out
   of the domain layer is what lets a future HTTP endpoint return 422
   for the same error without any change here.

Why `Finding` here and not shared with the other modules?
---------------------------------------------------------
The *concept* is shared. The *type* is not, on purpose. Each phase
declares its own `Finding` dataclass with phase-appropriate defaults
and any phase-specific extras (safety's findings carry a `root`; the
loader's carry `line`/`column`). Sharing one `Finding` across phases
would force every phase to carry every phase's optional fields and
would couple unrelated refactors: renaming `line` in the loader would
require touching rendering, planning, safety, and so on. Duplication
of a four-field dataclass is cheap; coupling of five unrelated error
domains is not. When the intersection stabilises (it likely will), the
sensible move is to *lift* the shared subset into a common protocol,
not to force every phase to import each other's types. That decision is
deferred until a second phase actually needs the same shape — right
now, only the CLI's generic renderer does, and it is deliberately
duck-typed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Finding:
    """One staging failure, self-contained.

    Attributes
    ----------
    path
        *Destination-relative* path of the file the failure concerns
        (e.g. ``"src/demo/main.py"``). This is what the user typed or
        what the plan promised — never an internal staging path, never
        an absolute path inside the tool's scratch directory. The rule
        is the same as everywhere else in the codebase: reports match
        what the user can fix.

        For structural failures that are not tied to a single file
        (e.g. a manifest mismatch that concerns the whole tree), use the
        closest sensible relative path, or ``"<staging>"`` if no single
        path applies. An empty string is reserved for cases where the
        failure genuinely has no path — but such cases are rare and
        should be reviewed: if a finding has no location, either it is
        a tool bug (and therefore belongs in a different error class),
        or the location is derivable (and therefore should be filled
        in).
    code
        Machine-readable classifier. The vocabulary for staging is:

            ``render.undefined_variable``
                A template referenced a variable that was not supplied.
                Fixed by adding the variable to the plan's inputs.
            ``render.syntax_error``
                A template had invalid Jinja syntax. Fixed by editing
                the template.
            ``render.sandbox_denied``
                A template attempted something the sandbox forbids
                (attribute access to Python internals, etc.).
            ``render.include_not_found``
                A template included another template that does not
                exist.
            ``render.io_encoding``
                A template was not valid UTF-8.
            ``copy.not_found``
                A ``kind=copy`` source file does not exist.
            ``copy.io_error``
                A ``kind=copy`` source file could not be read (permissions,
                I/O error).
            ``staging.missing``
                The staged tree is missing a file the plan declared.
                Always a staging-engine bug; the hint says so.
            ``staging.unexpected``
                The staged tree contains a file the plan did not
                declare. Always a staging-engine bug; the hint says so.

        The vocabulary is stable across releases of the staging engine.
        Adding codes is a minor change (documented in STAGING.md);
        renaming or removing codes is a breaking change requiring a
        contract note.

    message
        One-sentence, user-facing description of what went wrong. Never
        contains a stack trace, never begins with a phase name
        (``"staging: ..."``), never includes implementation details
        (line numbers, environment variables, Python function names).
        The audience is a blueprint author who has never read the
        staging engine's code.

    hint
        One or two sentences describing what the user can do about it.
        *Mandatory.* A `Finding` without a hint is a bug — the same rule
        the loader and dry-run enforce. Hints are phrased as imperatives
        ("Add …", "Check …", "Re-run with …") and never as questions
        ("Did you mean …?").

    Why frozen?
    -----------
    Findings are produced once and consumed many times (rendered by the
    CLI, serialised to JSON, embedded in a dry-run report, asserted on
    in tests). If a consumer mutates a finding, every subsequent
    consumer sees the mutated version, and the "which finding was it?"
    debugging question becomes unanswerable. Frozen dataclasses make
    mutation impossible; the caller must construct a new finding if it
    needs a variant. That is the correct cost.

    Why slots?
    ----------
    Findings are created in bulk on catastrophic failures (a plan with
    fifty broken templates yields fifty findings). `slots=True` removes
    the per-instance `__dict__` overhead, which matters at scale. The
    values are also small and fixed, which is exactly the shape `slots`
    is for.
    """

    code: str
    path: str
    message: str
    hint: str


@dataclass(frozen=True, slots=True)
class RenderingError(Exception):
    """Aggregated staging failure — one report, one exit decision.

    Attributes
    ----------
    findings
        Every failure observed in a single ``StagingEngine.stage()``
        call. Never empty on construction — an empty `RenderingError`
        would be a contradiction (an error with no errors). The engine
        only constructs this type when at least one file failed.

    operation
        Free-form label describing which phase produced the error
        (``"staging"`` today; future phases may reuse the type with a
        different label). Kept as a string, not an enum, because the
        set of phases is not closed — a new phase should be able to
        adopt the type without editing a central enum.

    Why is `operation` not a required field with no default?
    --------------------------------------------------------
    Defaults are dangerous for error types: the wrong default silently
    labels an error as something it is not. So `operation` has no
    default and must be supplied. The engine's call sites pass
    ``"staging"`` explicitly — a single word of verbosity in exchange
    for correctness in logs.

    Why does `RenderingError` inherit from `Exception` when the codebase
    otherwise prefers `Result`?
    ------------------------------------------------------------------
    Because the *engine* does not use exceptions to signal failure —
    ``stage()`` returns ``Result[StagingResult, RenderingError]``. The
    inheritance from ``Exception`` exists so that callers who *want* to
    raise (a script that prefers exceptions, a test helper, a future
    boundary) can do so without wrapping. It costs nothing to inherit,
    and it means the same object works in both idioms. See the loader's
    ``ValidationError`` for the same pattern: both are ``Exception``
    subclasses that are *returned* in practice and *raised* only at the
    outermost boundary. This keeps Result-vs-exception a caller's
    choice, not a type's.
    """

    findings: tuple[Finding, ...]
    operation: str = "staging"

    def __post_init__(self) -> None:
        # A `RenderingError` with no findings is a category error. If a
        # future refactor accidentally constructs one, this raises at
        # construction time (before the error escapes into a log or a
        # user report) rather than at the point where someone tries to
        # read `findings[0]` and gets an ``IndexError`` far from the
        # source of the mistake.
        #
        # `object.__setattr__` is not needed: `frozen=True` here prevents
        # *attribute* assignment, not reads. `__post_init__` may still
        # raise; that is the intended contract.
        if not self.findings:
            raise ValueError("RenderingError requires at least one finding")

    # -- presentation -----------------------------------------------------

    def __str__(self) -> str:
        """Human-readable summary, one block per finding.

        This format is intentionally close to the format every other
        phase's error uses. The CLI has its own renderer that adds colour
        and contextual hints (path to the blueprint, exit-code hint); the
        string here is a fallback for logs, `str(exc)` calls from within
        the tool itself, and any consumer that does not know about the
        CLI. It is not meant to be parsed — the JSON contract lives on
        the DTOs, not on error strings.
        """
        lines = [
            f"{len(self.findings)} rendering error(s) in {self.operation}:",
        ]
        for finding in self.findings:
            lines.append(
                f"  • [{finding.code}] {finding.path or '<staging>'}: " f"{finding.message}"
            )
            lines.append(f"      hint: {finding.hint}")
        return "\n".join(lines)

    def format(self) -> str:
        """Alias for ``str(self)``.

        Present because callers sometimes have a polymorphic
        ``err.format()`` call spanning several error types (loading,
        staging, safety) and a `.format()` method is more discoverable
        than relying on ``str()``. Both return the same string; neither
        is a stable contract for automation. Automation uses the
        ``Finding`` tuples directly or a JSON DTO.
        """
        return str(self)

    # -- ergonomics -------------------------------------------------------

    @property
    def codes(self) -> tuple[str, ...]:
        """Every finding code, in order.

        Convenience for tests and dashboards that need to check
        ``"render.undefined_variable" in err.codes`` without a
        comprehension. Ordering follows the aggregated findings, which
        the engine produces in plan order.
        """
        return tuple(f.code for f in self.findings)

    def __len__(self) -> int:
        """Number of findings, mirroring the underlying tuple."""
        return len(self.findings)

    def __iter__(self) -> Iterator[Finding]:
        """Iterate findings directly, so callers can write
        ``for finding in err:`` instead of ``for finding in err.findings:``
        when the distinction does not matter to them.

        The return type is ``Iterator[Finding]`` — the abstract
        iterable protocol — rather than the concrete ``tuple_iterator``
        that ``iter(self.findings)`` actually produces. The abstract
        type is what callers can rely on across Python versions and
        across reimplementations of the underlying storage (a future
        refactor that changed ``findings`` from a tuple to a frozen
        list would not need to touch this annotation, because both
        produce ``Iterator[Finding]``).
        """
        return iter(self.findings)
